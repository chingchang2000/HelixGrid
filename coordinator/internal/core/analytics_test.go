package core

import (
	"math"
	"testing"
	"time"
)

func analyticsSpec() WorkflowSpec {
	return WorkflowSpec{Name: "analytics", Tasks: []TaskSpec{
		{ID: "root", Command: []string{"true"}},
		{ID: "fast", DependsOn: []string{"root"}, Command: []string{"true"}},
		{ID: "slow", DependsOn: []string{"root"}, Command: []string{"true"}},
		{ID: "side", DependsOn: []string{"root"}, Command: []string{"true"}},
		{ID: "join", DependsOn: []string{"fast", "slow"}, Command: []string{"true"}},
		{ID: "end", DependsOn: []string{"join", "side"}, Command: []string{"true"}},
	}}
}

func TestAnalyzeGraph(t *testing.T) {
	analysis, err := AnalyzeGraph(analyticsSpec(), map[string]int64{
		"root": 1,
		"fast": 2,
		"slow": 10,
		"side": 1,
		"join": 3,
		"end": 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if analysis.TaskCount != 6 {
		t.Fatalf("TaskCount=%d", analysis.TaskCount)
	}
	if analysis.EdgeCount != 7 {
		t.Fatalf("EdgeCount=%d", analysis.EdgeCount)
	}
	if len(analysis.Roots) != 1 || analysis.Roots[0] != "root" {
		t.Fatalf("roots=%v", analysis.Roots)
	}
	if len(analysis.Sinks) != 1 || analysis.Sinks[0] != "end" {
		t.Fatalf("sinks=%v", analysis.Sinks)
	}
	wantPath := []string{"root", "slow", "join", "end"}
	if len(analysis.CriticalPath) != len(wantPath) {
		t.Fatalf("critical=%v", analysis.CriticalPath)
	}
	for i := range wantPath {
		if analysis.CriticalPath[i] != wantPath[i] {
			t.Fatalf("critical=%v", analysis.CriticalPath)
		}
	}
	if analysis.CriticalPathWeight != 15 {
		t.Fatalf("weight=%d", analysis.CriticalPathWeight)
	}
	if analysis.MaxParallelism != 3 {
		t.Fatalf("MaxParallelism=%d", analysis.MaxParallelism)
	}
	if analysis.MaxInDegree != 2 || analysis.MaxOutDegree != 3 {
		t.Fatalf("degrees in=%d out=%d", analysis.MaxInDegree, analysis.MaxOutDegree)
	}
	if analysis.DescendantsByTask["root"] != 5 {
		t.Fatalf("root descendants=%d", analysis.DescendantsByTask["root"])
	}
	if len(analysis.Bottlenecks) == 0 || analysis.Bottlenecks[0].TaskID != "root" {
		t.Fatalf("bottlenecks=%v", analysis.Bottlenecks)
	}
}

func TestAnalyzeGraphRejectsCycle(t *testing.T) {
	_, err := AnalyzeGraph(WorkflowSpec{Name: "cycle", Tasks: []TaskSpec{
		{ID: "a", DependsOn: []string{"b"}, Command: []string{"true"}},
		{ID: "b", DependsOn: []string{"a"}, Command: []string{"true"}},
	}}, nil)
	if err == nil {
		t.Fatal("expected cycle error")
	}
}

func TestAnalyzeRuntime(t *testing.T) {
	created := time.Unix(100, 0).UTC()
	workflow, err := NewWorkflow("wf", analyticsSpec(), created)
	if err != nil {
		t.Fatal(err)
	}
	workflow.Recompute(created)
	workflow.Runtime["root"].State = TaskSucceeded
	rootStart := created.Add(time.Second)
	rootEnd := created.Add(4 * time.Second)
	workflow.Runtime["root"].StartedAt = &rootStart
	workflow.Runtime["root"].FinishedAt = &rootEnd
	workflow.Runtime["root"].Attempt = 1
	workflow.Runtime["root"].OutputBytes = 100
	workflow.Recompute(created.Add(5 * time.Second))

	workflow.Runtime["fast"].State = TaskRunning
	fastStart := created.Add(5 * time.Second)
	workflow.Runtime["fast"].StartedAt = &fastStart
	workflow.Runtime["fast"].Attempt = 2
	workflow.Runtime["fast"].OutputBytes = 50
	workflow.Runtime["slow"].State = TaskReady
	workflow.Runtime["slow"].Attempt = 0
	workflow.Runtime["side"].State = TaskReady

	analysis, err := AnalyzeRuntime(workflow, created.Add(12*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if analysis.Attempts != 3 {
		t.Fatalf("attempts=%d", analysis.Attempts)
	}
	if analysis.Retries != 1 {
		t.Fatalf("retries=%d", analysis.Retries)
	}
	if analysis.OutputBytes != 150 {
		t.Fatalf("output=%d", analysis.OutputBytes)
	}
	if len(analysis.ReadyTasks) != 2 || analysis.ReadyTasks[0] != "side" || analysis.ReadyTasks[1] != "slow" {
		t.Fatalf("ready=%v", analysis.ReadyTasks)
	}
	if len(analysis.ActiveTasks) != 1 || analysis.ActiveTasks[0] != "fast" {
		t.Fatalf("active=%v", analysis.ActiveTasks)
	}
	if analysis.LongestTask == nil || analysis.LongestTask.TaskID != "fast" || analysis.LongestTask.Duration != 7*time.Second {
		t.Fatalf("longest=%+v", analysis.LongestTask)
	}
	if analysis.Elapsed != 12*time.Second {
		t.Fatalf("elapsed=%v", analysis.Elapsed)
	}
}

func TestRuntimeBlockedExplanation(t *testing.T) {
	now := time.Now().UTC()
	workflow, err := NewWorkflow("wf", WorkflowSpec{Name: "blocked", Tasks: []TaskSpec{
		{ID: "first", Command: []string{"true"}},
		{ID: "second", DependsOn: []string{"first"}, Command: []string{"true"}},
	}}, now)
	if err != nil {
		t.Fatal(err)
	}
	workflow.Recompute(now)
	analysis, err := AnalyzeRuntime(workflow, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(analysis.BlockedTasks) != 1 {
		t.Fatalf("blocked=%v", analysis.BlockedTasks)
	}
	if analysis.BlockedTasks[0].TaskID != "second" || len(analysis.BlockedTasks[0].WaitingOn) != 1 || analysis.BlockedTasks[0].WaitingOn[0] != "first" {
		t.Fatalf("blocked=%+v", analysis.BlockedTasks[0])
	}
}

func TestEstimateMakespan(t *testing.T) {
	durations := map[string]time.Duration{
		"root": time.Second,
		"fast": 2 * time.Second,
		"slow": 10 * time.Second,
		"side": 5 * time.Second,
		"join": 3 * time.Second,
		"end": time.Second,
	}
	one, err := EstimateMakespan(analyticsSpec(), durations, 1)
	if err != nil {
		t.Fatal(err)
	}
	if one != 22*time.Second {
		t.Fatalf("one worker=%v", one)
	}
	many, err := EstimateMakespan(analyticsSpec(), durations, 8)
	if err != nil {
		t.Fatal(err)
	}
	if many != 15*time.Second {
		t.Fatalf("many workers=%v", many)
	}
	if many >= one {
		t.Fatalf("parallel estimate did not improve: one=%v many=%v", one, many)
	}
}

func TestEstimateMakespanRejectsBadInputs(t *testing.T) {
	if _, err := EstimateMakespan(analyticsSpec(), nil, 0); err == nil {
		t.Fatal("expected worker count error")
	}
	if _, err := EstimateMakespan(analyticsSpec(), map[string]time.Duration{"root": -time.Second}, 2); err == nil {
		t.Fatal("expected negative duration error")
	}
}

func TestSaturation(t *testing.T) {
	workers := []*Worker{
		{Capacity: 4, ActiveLeases: 3},
		{Capacity: 2, ActiveLeases: 1},
		nil,
		{Capacity: 0, ActiveLeases: 100},
	}
	got := Saturation(workers)
	want := 4.0 / 6.0
	if math.Abs(got-want) > 1e-9 {
		t.Fatalf("saturation=%f want=%f", got, want)
	}
	if Saturation(nil) != 0 {
		t.Fatal("empty worker set should have zero saturation")
	}
}
