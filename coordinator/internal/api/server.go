package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/chingchang2000/app/coordinator/internal/core"
)

type Server struct {
	store  *core.Store
	logger *slog.Logger
	mux    *http.ServeMux
}

type errorResponse struct { Error string `json:"error"` }
type leaseRequest struct { WorkerID string `json:"worker_id"` }
type logRequest struct { Stream string `json:"stream"`; Text string `json:"text"` }

type APIEnvelope[T any] struct {
	Data T `json:"data"`
}

func New(store *core.Store, logger *slog.Logger) *Server {
	if logger == nil { logger = slog.Default() }
	s := &Server{store: store, logger: logger, mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) Handler() http.Handler {
	return requestID(logging(recoverer(cors(s.mux)), s.logger))
}

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, http.StatusOK, map[string]any{"status":"ok","time":time.Now().UTC()}) })
	s.mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, http.StatusOK, map[string]any{"ready":true}) })
	s.mux.HandleFunc("POST /v1/workflows", s.createWorkflow)
	s.mux.HandleFunc("GET /v1/workflows", s.listWorkflows)
	s.mux.HandleFunc("GET /v1/workflows/{id}", s.getWorkflow)
	s.mux.HandleFunc("POST /v1/workflows/{id}/cancel", s.cancelWorkflow)
	s.mux.HandleFunc("GET /v1/workflows/{id}/events", s.workflowEvents)
	s.mux.HandleFunc("POST /v1/workers/register", s.registerWorker)
	s.mux.HandleFunc("GET /v1/workers", s.listWorkers)
	s.mux.HandleFunc("POST /v1/workers/{id}/heartbeat", s.heartbeatWorker)
	s.mux.HandleFunc("POST /v1/leases", s.leaseNext)
	s.mux.HandleFunc("POST /v1/leases/{token}/start", s.startLease)
	s.mux.HandleFunc("POST /v1/leases/{token}/renew", s.renewLease)
	s.mux.HandleFunc("POST /v1/leases/{token}/logs", s.appendLog)
	s.mux.HandleFunc("POST /v1/leases/{token}/complete", s.completeLease)
}

func decodeJSON[T any](w http.ResponseWriter, r *http.Request) (T, bool) {
	var value T
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&value); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return value, false
	}
	return value, true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, errorResponse{Error: message})
}

func (s *Server) createWorkflow(w http.ResponseWriter, r *http.Request) {
	spec, ok := decodeJSON[core.WorkflowSpec](w, r); if !ok { return }
	created, fresh, err := s.store.CreateWorkflow(spec, r.Header.Get("Idempotency-Key"))
	if err != nil { writeError(w, http.StatusUnprocessableEntity, err.Error()); return }
	status := http.StatusOK; if fresh { status = http.StatusCreated }
	writeJSON(w, status, APIEnvelope[*core.Workflow]{Data: created})
}

func (s *Server) listWorkflows(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, APIEnvelope[[]*core.Workflow]{Data: s.store.ListWorkflows()})
}

func (s *Server) getWorkflow(w http.ResponseWriter, r *http.Request) {
	item, ok := s.store.GetWorkflow(r.PathValue("id")); if !ok { writeError(w, http.StatusNotFound, "workflow not found"); return }
	writeJSON(w, http.StatusOK, APIEnvelope[*core.Workflow]{Data:item})
}

func (s *Server) cancelWorkflow(w http.ResponseWriter, r *http.Request) {
	item, err := s.store.CancelWorkflow(r.PathValue("id")); if err != nil { writeError(w, http.StatusNotFound, err.Error()); return }
	writeJSON(w, http.StatusOK, APIEnvelope[*core.Workflow]{Data:item})
}

func (s *Server) registerWorker(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeJSON[core.RegisterWorkerRequest](w, r); if !ok { return }
	if strings.TrimSpace(req.Name) == "" { writeError(w, http.StatusBadRequest, "worker name is required"); return }
	worker := s.store.RegisterWorker(req)
	writeJSON(w, http.StatusCreated, APIEnvelope[*core.Worker]{Data:worker})
}

func (s *Server) listWorkers(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, APIEnvelope[[]*core.Worker]{Data:s.store.ListWorkers()})
}

func (s *Server) heartbeatWorker(w http.ResponseWriter, r *http.Request) {
	worker, err := s.store.HeartbeatWorker(r.PathValue("id")); if err != nil { writeError(w, http.StatusNotFound, err.Error()); return }
	writeJSON(w, http.StatusOK, APIEnvelope[*core.Worker]{Data:worker})
}

func (s *Server) leaseNext(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeJSON[leaseRequest](w, r); if !ok { return }
	lease, err := s.store.LeaseNext(req.WorkerID)
	if err != nil { writeError(w, http.StatusBadRequest, err.Error()); return }
	if lease == nil { w.WriteHeader(http.StatusNoContent); return }
	writeJSON(w, http.StatusOK, APIEnvelope[*core.Lease]{Data:lease})
}

func (s *Server) startLease(w http.ResponseWriter, r *http.Request) {
	if err := s.store.StartLease(r.PathValue("token")); err != nil { writeError(w, http.StatusConflict, err.Error()); return }
	writeJSON(w, http.StatusOK, map[string]bool{"ok":true})
}

func (s *Server) renewLease(w http.ResponseWriter, r *http.Request) {
	lease, err := s.store.RenewLease(r.PathValue("token")); if err != nil { writeError(w, http.StatusConflict, err.Error()); return }
	writeJSON(w, http.StatusOK, APIEnvelope[*core.Lease]{Data:lease})
}

func (s *Server) appendLog(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeJSON[logRequest](w, r); if !ok { return }
	if req.Stream != "stdout" && req.Stream != "stderr" { writeError(w, http.StatusBadRequest, "stream must be stdout or stderr"); return }
	if err := s.store.AppendLog(r.PathValue("token"), req.Stream, req.Text); err != nil { writeError(w, http.StatusConflict, err.Error()); return }
	writeJSON(w, http.StatusAccepted, map[string]bool{"ok":true})
}

func (s *Server) completeLease(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeJSON[core.CompleteRequest](w, r); if !ok { return }
	workflow, err := s.store.CompleteLease(r.PathValue("token"), req); if err != nil { writeError(w, http.StatusConflict, err.Error()); return }
	writeJSON(w, http.StatusOK, APIEnvelope[*core.Workflow]{Data:workflow})
}

func (s *Server) workflowEvents(w http.ResponseWriter, r *http.Request) {
	workflowID := r.PathValue("id")
	if _, ok := s.store.GetWorkflow(workflowID); !ok { writeError(w, http.StatusNotFound, "workflow not found"); return }
	flusher, ok := w.(http.Flusher); if !ok { writeError(w, http.StatusInternalServerError, "streaming unsupported"); return }
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	after := int64(0)
	if raw := r.Header.Get("Last-Event-ID"); raw != "" { after, _ = strconv.ParseInt(raw, 10, 64) }
	for _, event := range s.store.Events().Replay(workflowID, after) { if err := sendSSE(w, event); err != nil { return } }
	flusher.Flush()
	_, events, cancel := s.store.Events().Subscribe(256); defer cancel()
	ping := time.NewTicker(15*time.Second); defer ping.Stop()
	for {
		select {
		case <-r.Context().Done(): return
		case event, open := <-events:
			if !open { return }
			if event.WorkflowID != workflowID { continue }
			if err := sendSSE(w, event); err != nil { return }; flusher.Flush()
		case <-ping.C:
			_, _ = fmt.Fprint(w, ": ping\n\n"); flusher.Flush()
		}
	}
}

func sendSSE(w http.ResponseWriter, event core.Event) error {
	payload, err := json.Marshal(event); if err != nil { return err }
	_, err = fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", event.ID, event.Type, payload)
	return err
}

type contextKey string
const requestIDKey contextKey = "request-id"

func requestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-ID")
		if id == "" { id = fmt.Sprintf("req-%d", time.Now().UnixNano()) }
		w.Header().Set("X-Request-ID", id)
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), requestIDKey, id)))
	})
}

func logging(next http.Handler, logger *slog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now(); rw := &statusWriter{ResponseWriter:w, status:200}
		next.ServeHTTP(rw, r)
		logger.Info("http request", "method",r.Method,"path",r.URL.Path,"status",rw.status,"bytes",rw.bytes,"duration_ms",time.Since(start).Milliseconds(),"request_id",r.Context().Value(requestIDKey))
	})
}

type statusWriter struct { http.ResponseWriter; status int; bytes int }
func (w *statusWriter) WriteHeader(code int) { w.status=code; w.ResponseWriter.WriteHeader(code) }
func (w *statusWriter) Write(p []byte) (int,error) { n,err:=w.ResponseWriter.Write(p); w.bytes+=n; return n,err }

func recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func(){ if v:=recover(); v!=nil { writeError(w,http.StatusInternalServerError,"internal server error") } }()
		next.ServeHTTP(w,r)
	})
}

func cors(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin","*")
		w.Header().Set("Access-Control-Allow-Headers","Content-Type, Idempotency-Key, Last-Event-ID, X-Request-ID")
		w.Header().Set("Access-Control-Allow-Methods","GET,POST,OPTIONS")
		if r.Method==http.MethodOptions { w.WriteHeader(http.StatusNoContent); return }
		next.ServeHTTP(w,r)
	})
}

var _ = errors.New
