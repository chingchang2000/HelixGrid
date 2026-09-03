package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"math/rand/v2"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type config struct {
	baseURL       string
	clients       int
	duration      time.Duration
	tasks         int
	fanout        int
	requestTimeout time.Duration
	seedPrefix    string
	jsonOutput    bool
}

type taskSpec struct {
	ID        string   `json:"id"`
	DependsOn []string `json:"depends_on,omitempty"`
	Command   []string `json:"command"`
}

type workflowSpec struct {
	Name     string            `json:"name"`
	Metadata map[string]string `json:"metadata,omitempty"`
	Tasks    []taskSpec        `json:"tasks"`
}

type workflow struct {
	ID    string `json:"id"`
	State string `json:"state"`
}

type envelope[T any] struct {
	Data T `json:"data"`
}

type sample struct {
	operation string
	latency   time.Duration
	status    int
	err       string
}

type counters struct {
	requests atomic.Uint64
	errors   atomic.Uint64
	bytesIn  atomic.Uint64
	bytesOut atomic.Uint64
}

type aggregate struct {
	Operation string  `json:"operation"`
	Count     int     `json:"count"`
	Errors    int     `json:"errors"`
	P50MS     float64 `json:"p50_ms"`
	P90MS     float64 `json:"p90_ms"`
	P95MS     float64 `json:"p95_ms"`
	P99MS     float64 `json:"p99_ms"`
	MaxMS     float64 `json:"max_ms"`
	MeanMS    float64 `json:"mean_ms"`
}

type report struct {
	DurationSeconds float64     `json:"duration_seconds"`
	Clients         int         `json:"clients"`
	Requests        uint64      `json:"requests"`
	Errors          uint64      `json:"errors"`
	RequestsPerSec  float64     `json:"requests_per_second"`
	BytesIn         uint64      `json:"bytes_in"`
	BytesOut        uint64      `json:"bytes_out"`
	Operations      []aggregate `json:"operations"`
}

func main() {
	cfg := parseFlags()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, cfg.duration)
	defer cancel()

	client := &http.Client{
		Timeout: cfg.requestTimeout,
		Transport: &http.Transport{
			MaxIdleConns:        cfg.clients * 4,
			MaxIdleConnsPerHost: cfg.clients * 4,
			IdleConnTimeout:     60 * time.Second,
			ForceAttemptHTTP2:   true,
		},
	}

	if err := readiness(ctx, client, cfg.baseURL); err != nil {
		fmt.Fprintln(os.Stderr, "helix-loadgen:", err)
		os.Exit(2)
	}

	results := make(chan sample, 8192)
	var stats counters
	var workers sync.WaitGroup
	started := time.Now()
	for i := 0; i < cfg.clients; i++ {
		workers.Add(1)
		go func(index int) {
			defer workers.Done()
			runClient(ctx, client, cfg, index, results, &stats)
		}(i)
	}

	go func() {
		workers.Wait()
		close(results)
	}()

	all := make([]sample, 0, 100_000)
	for item := range results {
		all = append(all, item)
	}
	elapsed := time.Since(started)
	report := buildReport(cfg.clients, elapsed, all, &stats)
	if cfg.jsonOutput {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(report)
		return
	}
	printHuman(report)
}

func parseFlags() config {
	var cfg config
	flag.StringVar(&cfg.baseURL, "url", "http://127.0.0.1:8080", "coordinator base URL")
	flag.IntVar(&cfg.clients, "clients", 16, "number of concurrent virtual clients")
	flag.DurationVar(&cfg.duration, "duration", 30*time.Second, "test duration")
	flag.IntVar(&cfg.tasks, "tasks", 24, "tasks per submitted workflow")
	flag.IntVar(&cfg.fanout, "fanout", 4, "maximum dependency fanout")
	flag.DurationVar(&cfg.requestTimeout, "request-timeout", 5*time.Second, "HTTP request timeout")
	flag.StringVar(&cfg.seedPrefix, "prefix", "load", "workflow name/idempotency prefix")
	flag.BoolVar(&cfg.jsonOutput, "json", false, "print JSON report")
	flag.Parse()
	cfg.baseURL = strings.TrimRight(cfg.baseURL, "/")
	if cfg.clients < 1 || cfg.clients > 10_000 {
		fatal("clients must be between 1 and 10000")
	}
	if cfg.tasks < 1 || cfg.tasks > 10_000 {
		fatal("tasks must be between 1 and 10000")
	}
	if cfg.fanout < 1 || cfg.fanout > 128 {
		fatal("fanout must be between 1 and 128")
	}
	if cfg.duration <= 0 {
		fatal("duration must be positive")
	}
	return cfg
}

func fatal(message string) {
	fmt.Fprintln(os.Stderr, "helix-loadgen:", message)
	os.Exit(2)
}

func readiness(ctx context.Context, client *http.Client, baseURL string) error {
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/readyz", nil)
		if err != nil {
			return err
		}
		resp, err := client.Do(req)
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline.C:
			return errors.New("coordinator did not become ready within 5 seconds")
		case <-ticker.C:
		}
	}
}

func runClient(ctx context.Context, client *http.Client, cfg config, index int, out chan<- sample, stats *counters) {
	sequence := uint64(0)
	known := make([]string, 0, 128)
	for {
		if ctx.Err() != nil {
			return
		}
		sequence++
		roll := rand.IntN(100)
		switch {
		case len(known) == 0 || roll < 42:
			name := fmt.Sprintf("%s-c%03d-%08d", cfg.seedPrefix, index, sequence)
			spec := makeWorkflow(name, cfg.tasks, cfg.fanout)
			var response envelope[workflow]
			status, latency, inBytes, outBytes, err := doJSON(ctx, client, http.MethodPost, cfg.baseURL+"/v1/workflows", spec, &response, map[string]string{"Idempotency-Key": name})
			stats.requests.Add(1)
			stats.bytesIn.Add(uint64(max(inBytes, 0)))
			stats.bytesOut.Add(uint64(max(outBytes, 0)))
			item := sample{operation: "submit", latency: latency, status: status}
			if err != nil {
				stats.errors.Add(1)
				item.err = err.Error()
			} else if response.Data.ID != "" {
				known = append(known, response.Data.ID)
				if len(known) > 256 {
					known = known[len(known)-256:]
				}
			}
			emit(ctx, out, item)

		case roll < 78:
			id := known[rand.IntN(len(known))]
			var response envelope[workflow]
			status, latency, inBytes, outBytes, err := doJSON(ctx, client, http.MethodGet, cfg.baseURL+"/v1/workflows/"+id, nil, &response, nil)
			stats.requests.Add(1)
			stats.bytesIn.Add(uint64(max(inBytes, 0)))
			stats.bytesOut.Add(uint64(max(outBytes, 0)))
			item := sample{operation: "get", latency: latency, status: status}
			if err != nil {
				stats.errors.Add(1)
				item.err = err.Error()
			}
			emit(ctx, out, item)

		case roll < 94:
			var response envelope[[]workflow]
			status, latency, inBytes, outBytes, err := doJSON(ctx, client, http.MethodGet, cfg.baseURL+"/v1/workflows", nil, &response, nil)
			stats.requests.Add(1)
			stats.bytesIn.Add(uint64(max(inBytes, 0)))
			stats.bytesOut.Add(uint64(max(outBytes, 0)))
			item := sample{operation: "list", latency: latency, status: status}
			if err != nil {
				stats.errors.Add(1)
				item.err = err.Error()
			}
			emit(ctx, out, item)

		default:
			id := known[rand.IntN(len(known))]
			var response envelope[workflow]
			status, latency, inBytes, outBytes, err := doJSON(ctx, client, http.MethodPost, cfg.baseURL+"/v1/workflows/"+id+"/cancel", nil, &response, nil)
			stats.requests.Add(1)
			stats.bytesIn.Add(uint64(max(inBytes, 0)))
			stats.bytesOut.Add(uint64(max(outBytes, 0)))
			item := sample{operation: "cancel", latency: latency, status: status}
			if err != nil {
				stats.errors.Add(1)
				item.err = err.Error()
			}
			emit(ctx, out, item)
		}
	}
}

func emit(ctx context.Context, out chan<- sample, item sample) {
	select {
	case out <- item:
	case <-ctx.Done():
	}
}

func makeWorkflow(name string, count, fanout int) workflowSpec {
	tasks := make([]taskSpec, 0, count)
	for i := 0; i < count; i++ {
		id := fmt.Sprintf("task-%04d", i)
		deps := make([]string, 0, fanout)
		if i > 0 {
			start := max(0, i-fanout)
			for parent := start; parent < i; parent++ {
				// A deterministic sparse edge pattern creates fan-out/fan-in graphs without
				// making every task depend on the entire prefix.
				if parent == i-1 || (i+parent)%3 == 0 {
					deps = append(deps, fmt.Sprintf("task-%04d", parent))
				}
			}
		}
		tasks = append(tasks, taskSpec{ID: id, DependsOn: deps, Command: []string{"sh", "-lc", "true"}})
	}
	return workflowSpec{Name: name, Metadata: map[string]string{"source": "helix-loadgen", "tasks": strconv.Itoa(count)}, Tasks: tasks}
}

func doJSON(ctx context.Context, client *http.Client, method, url string, requestBody any, responseBody any, extraHeaders map[string]string) (status int, latency time.Duration, bytesIn, bytesOut int, err error) {
	var body io.Reader
	if requestBody != nil {
		raw, marshalErr := json.Marshal(requestBody)
		if marshalErr != nil {
			return 0, 0, 0, 0, marshalErr
		}
		bytesOut = len(raw)
		body = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return 0, 0, 0, bytesOut, err
	}
	req.Header.Set("Accept", "application/json")
	if requestBody != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for key, value := range extraHeaders {
		req.Header.Set(key, value)
	}
	started := time.Now()
	resp, err := client.Do(req)
	latency = time.Since(started)
	if err != nil {
		return 0, latency, 0, bytesOut, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	bytesIn = len(raw)
	if err != nil {
		return resp.StatusCode, latency, bytesIn, bytesOut, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return resp.StatusCode, latency, bytesIn, bytesOut, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	if responseBody != nil && len(raw) > 0 {
		if err := json.Unmarshal(raw, responseBody); err != nil {
			return resp.StatusCode, latency, bytesIn, bytesOut, fmt.Errorf("decode response: %w", err)
		}
	}
	return resp.StatusCode, latency, bytesIn, bytesOut, nil
}

func buildReport(clients int, duration time.Duration, samples []sample, stats *counters) report {
	byOperation := map[string][]sample{}
	for _, item := range samples {
		byOperation[item.operation] = append(byOperation[item.operation], item)
	}
	operations := make([]aggregate, 0, len(byOperation))
	for name, items := range byOperation {
		operations = append(operations, aggregateSamples(name, items))
	}
	sort.Slice(operations, func(i, j int) bool { return operations[i].Operation < operations[j].Operation })
	seconds := duration.Seconds()
	requestCount := stats.requests.Load()
	return report{
		DurationSeconds: seconds,
		Clients: clients,
		Requests: requestCount,
		Errors: stats.errors.Load(),
		RequestsPerSec: float64(requestCount) / max(seconds, 0.000001),
		BytesIn: stats.bytesIn.Load(),
		BytesOut: stats.bytesOut.Load(),
		Operations: operations,
	}
}

func aggregateSamples(name string, items []sample) aggregate {
	latencies := make([]float64, 0, len(items))
	errors := 0
	total := 0.0
	for _, item := range items {
		ms := float64(item.latency) / float64(time.Millisecond)
		latencies = append(latencies, ms)
		total += ms
		if item.err != "" || item.status >= 400 {
			errors++
		}
	}
	sort.Float64s(latencies)
	mean := 0.0
	if len(latencies) > 0 {
		mean = total / float64(len(latencies))
	}
	return aggregate{
		Operation: name,
		Count: len(items),
		Errors: errors,
		P50MS: percentile(latencies, 0.50),
		P90MS: percentile(latencies, 0.90),
		P95MS: percentile(latencies, 0.95),
		P99MS: percentile(latencies, 0.99),
		MaxMS: percentile(latencies, 1.0),
		MeanMS: mean,
	}
}

func percentile(sortedValues []float64, p float64) float64 {
	if len(sortedValues) == 0 {
		return 0
	}
	if p <= 0 {
		return sortedValues[0]
	}
	if p >= 1 {
		return sortedValues[len(sortedValues)-1]
	}
	position := p * float64(len(sortedValues)-1)
	lower := int(math.Floor(position))
	upper := int(math.Ceil(position))
	if lower == upper {
		return sortedValues[lower]
	}
	fraction := position - float64(lower)
	return sortedValues[lower]*(1-fraction) + sortedValues[upper]*fraction
}

func printHuman(r report) {
	fmt.Printf("HelixGrid load test\n")
	fmt.Printf("==============================================================\n")
	fmt.Printf("duration      %8.2fs\n", r.DurationSeconds)
	fmt.Printf("clients       %8d\n", r.Clients)
	fmt.Printf("requests      %8d\n", r.Requests)
	fmt.Printf("errors        %8d\n", r.Errors)
	fmt.Printf("throughput    %8.1f req/s\n", r.RequestsPerSec)
	fmt.Printf("traffic       %8.2f MiB in / %.2f MiB out\n", float64(r.BytesIn)/(1024*1024), float64(r.BytesOut)/(1024*1024))
	fmt.Println()
	fmt.Printf("%-10s %8s %7s %9s %9s %9s %9s %9s\n", "operation", "count", "errors", "mean", "p50", "p95", "p99", "max")
	for _, op := range r.Operations {
		fmt.Printf("%-10s %8d %7d %8.2fms %8.2fms %8.2fms %8.2fms %8.2fms\n", op.Operation, op.Count, op.Errors, op.MeanMS, op.P50MS, op.P95MS, op.P99MS, op.MaxMS)
	}
}
