package core

import (
	"testing"
	"time"
)

func TestValidateSpecTopologicalOrder(t *testing.T) {
	spec := WorkflowSpec{Name: "build", Tasks: []TaskSpec{
		{ID: "publish", DependsOn: []string{"package"}, Command: []string{"true"}},
		{ID: "test", DependsOn: []string{"prepare"}, Command: []string{"true"}},
		{ID: "prepare", Command: []string{"true"}},
		{ID: "package", DependsOn: []string{"test", "lint"}, Command: []string{"true"}},
		{ID: "lint", DependsOn: []string{"prepare"}, Command: []string{"true"}},
	}}
	order, err := ValidateSpec(spec)
	if err != nil { t.Fatalf("ValidateSpec: %v", err) }
	position := map[string]int{}
	for i, id := range order { position[id] = i }
	for _, task := range spec.Tasks {
		for _, dep := range task.DependsOn {
			if position[dep] >= position[task.ID] {
				t.Fatalf("dependency %q must precede %q: %v", dep, task.ID, order)
			}
	}	}
}

func TestValidateSpecRejectsCycles(t *testing.T) {
	_, err := ValidateSpec(WorkflowSpec{Name: "cycle", Tasks: []TaskSpec{
		{ID:"a", DependsOn:[]string{"c"}, Command:[]string{"true"}},
		{ID:"b", DependsOn:[]string{"a"}, Command:[]string{"true"}},
		{ID:"c", DependsOn:[]string{"b"}, Command:[]string{"true"}},
	}})
	if err == nil { t.Fatal("expected dependency cycle to fail") }
}

func TestValidateSpecRejectsUnknownDependencies(t *testing.T) {
	_, err := ValidateSpec(WorkflowSpec{Name:"bad", Tasks:[]TaskSpec{{ID:"a", DependsOn:[]string{"missing"}, Command:[]string{"true"}}}})
	if err == nil { t.Fatal("expected unknown dependency to fail") }
}

func TestRetryDelayIsBoundedExponential(t *testing.T) {
	p := RetryPolicy{MaxAttempts:10, BaseDelayMS:100, MaxDelayMS:450}
	cases := []struct{ attempt int; want time.Duration }{
		{1,100*time.Millisecond},
		{2,200*time.Millisecond},
		{3,400*time.Millisecond},
		{4,450*time.Millisecond},
		{9,450*time.Millisecond},
	}
	for _, tc := range cases {
		if got := RetryDelay(p, tc.attempt); got != tc.want { t.Errorf("attempt %d: got %s want %s",tc.attempt,got,tc.want) }
	}
}

func TestWorkflowRecomputeFanOutFanIn(t *testing.T) {
	now := time.Unix(100,0).UTC()
	w, err := NewWorkflow("wf_test", WorkflowSpec{Name:"pipeline", Tasks:[]TaskSpec{
		{ID:"root", Command:[]string{"true"}},
		{ID:"left", DependsOn:[]string{"root"}, Command:[]string{"true"}},
		{ID:"right", DependsOn:[]string{"root"}, Command:[]string{"true"}},
		{ID:"join", DependsOn:[]string{"left","right"}, Command:[]string{"true"}},
	}}, now)
	if err != nil { t.Fatal(err) }
	w.Recompute(now)
	if w.State != WorkflowRunning { t.Fatalf("workflow state=%s",w.State) }
	if w.Runtime["root"].State != TaskReady { t.Fatalf("root state=%s",w.Runtime["root"].State) }
	if w.Runtime["left"].State != TaskPending || w.Runtime["right"].State != TaskPending { t.Fatal("children became ready too early") }

	w.Runtime["root"].State = TaskSucceeded
	w.Recompute(now.Add(time.Second))
	if w.Runtime["left"].State != TaskReady || w.Runtime["right"].State != TaskReady { t.Fatal("fan-out did not become ready") }
	if w.Runtime["join"].State != TaskPending { t.Fatal("join became ready too early") }

	w.Runtime["left"].State = TaskSucceeded
	w.Recompute(now.Add(2*time.Second))
	if w.Runtime["join"].State != TaskPending { t.Fatal("join became ready with one dependency unfinished") }
	w.Runtime["right"].State = TaskSucceeded
	w.Recompute(now.Add(3*time.Second))
	if w.Runtime["join"].State != TaskReady { t.Fatalf("join state=%s",w.Runtime["join"].State) }
	w.Runtime["join"].State = TaskSucceeded
	w.Recompute(now.Add(4*time.Second))
	if w.State != WorkflowSucceeded { t.Fatalf("workflow state=%s",w.State) }
	if w.FinishedAt == nil { t.Fatal("terminal workflow missing FinishedAt") }
}

func TestCancellationIsMonotonic(t *testing.T) {
	now := time.Now().UTC()
	w, err := NewWorkflow("wf",WorkflowSpec{Name:"cancel",Tasks:[]TaskSpec{{ID:"a",Command:[]string{"true"}}}},now)
	if err != nil { t.Fatal(err) }
	w.Recompute(now)
	w.Cancel(now.Add(time.Second))
	if w.State != WorkflowCancelled || w.Runtime["a"].State != TaskCancelled { t.Fatal("workflow did not cancel") }
	w.Recompute(now.Add(2*time.Second))
	if w.State != WorkflowCancelled { t.Fatal("cancelled workflow changed state") }
}

func TestEventBusReplayAndSubscription(t *testing.T) {
	bus := NewEventBus(3)
	_, ch, cancel := bus.Subscribe(4)
	defer cancel()
	one := bus.Publish(Event{Type:EventWorkflowCreated,WorkflowID:"a"})
	bus.Publish(Event{Type:EventWorkflowCreated,WorkflowID:"b"})
	three := bus.Publish(Event{Type:EventTaskReady,WorkflowID:"a"})
	bus.Publish(Event{Type:EventTaskStarted,WorkflowID:"a"})

	if one.ID != 1 || three.ID != 3 { t.Fatalf("unexpected IDs: %d %d",one.ID,three.ID) }
	replay := bus.Replay("a",1)
	if len(replay) != 2 { t.Fatalf("replay len=%d want=2",len(replay)) }
	for i:=0;i<4;i++ { <-ch }
}
