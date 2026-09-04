package core

import (
	"testing"
	"time"
)

type fakeClock struct{ now time.Time }
func (c *fakeClock) Now() time.Time { return c.now }
func (c *fakeClock) Advance(d time.Duration) { c.now = c.now.Add(d) }

func testStore(t *testing.T) (*Store,*fakeClock) {
	t.Helper()
	clock := &fakeClock{now:time.Unix(1_700_000_000,0).UTC()}
	store := NewStore(NewEventBus(1000))
	store.clock = clock.Now
	store.leaseDuration = 10*time.Second
	store.workerTTL = 30*time.Second
	return store,clock
}

func simpleSpec() WorkflowSpec {
	return WorkflowSpec{Name:"test",Tasks:[]TaskSpec{
		{ID:"a",Command:[]string{"sh","-lc","true"},Retry:RetryPolicy{MaxAttempts:2,BaseDelayMS:100,MaxDelayMS:1000}},
		{ID:"b",DependsOn:[]string{"a"},Command:[]string{"sh","-lc","true"}},
	}}
}

func TestCreateWorkflowIdempotency(t *testing.T) {
	store,_ := testStore(t)
	first,fresh,err := store.CreateWorkflow(simpleSpec(),"same")
	if err != nil || !fresh { t.Fatalf("first create fresh=%v err=%v",fresh,err) }
	second,fresh,err := store.CreateWorkflow(simpleSpec(),"same")
	if err != nil || fresh { t.Fatalf("second create fresh=%v err=%v",fresh,err) }
	if first.ID != second.ID { t.Fatalf("ids differ: %s %s",first.ID,second.ID) }
	if got:=len(store.ListWorkflows()); got!=1 { t.Fatalf("workflows=%d",got) }
}

func TestLeaseCompleteUnlocksDependency(t *testing.T) {
	store,_ := testStore(t)
	workflow,_,err := store.CreateWorkflow(simpleSpec(),"")
	if err != nil { t.Fatal(err) }
	worker:=store.RegisterWorker(RegisterWorkerRequest{Name:"test",Version:"1",Capacity:1})
	lease,err:=store.LeaseNext(worker.ID)
	if err != nil || lease==nil { t.Fatalf("lease=%v err=%v",lease,err) }
	if lease.TaskID!="a" { t.Fatalf("leased %q",lease.TaskID) }
	if err:=store.StartLease(lease.Token); err!=nil { t.Fatal(err) }
	updated,err:=store.CompleteLease(lease.Token,CompleteRequest{ExitCode:0})
	if err!=nil { t.Fatal(err) }
	if updated.Runtime["a"].State!=TaskSucceeded { t.Fatalf("a=%s",updated.Runtime["a"].State) }
	if updated.Runtime["b"].State!=TaskReady { t.Fatalf("b=%s",updated.Runtime["b"].State) }
	lease2,err:=store.LeaseNext(worker.ID)
	if err!=nil || lease2==nil || lease2.TaskID!="b" { t.Fatalf("lease2=%v err=%v",lease2,err) }
	if workflow.ID!=lease2.WorkflowID { t.Fatal("leased task from wrong workflow") }
}

func TestFailedTaskRetriesThenFails(t *testing.T) {
	store,clock:=testStore(t)
	w,_,err:=store.CreateWorkflow(simpleSpec(),"")
	if err!=nil { t.Fatal(err) }
	worker:=store.RegisterWorker(RegisterWorkerRequest{Name:"w",Version:"1",Capacity:1})
	lease,_:=store.LeaseNext(worker.ID)
	_ = store.StartLease(lease.Token)
	updated,err:=store.CompleteLease(lease.Token,CompleteRequest{ExitCode:1,Error:"boom"})
	if err!=nil { t.Fatal(err) }
	if updated.Runtime["a"].State!=TaskRetryWait { t.Fatalf("state=%s",updated.Runtime["a"].State) }
	if updated.Runtime["a"].Attempt!=1 { t.Fatalf("attempt=%d",updated.Runtime["a"].Attempt) }
	if updated.Runtime["a"].NextRetryAt==nil { t.Fatal("missing retry time") }
	if leaseNow,_:=store.LeaseNext(worker.ID); leaseNow!=nil { t.Fatal("retry leased before backoff elapsed") }

	clock.Advance(101*time.Millisecond)
	store.Sweep()
	lease2,err:=store.LeaseNext(worker.ID)
	if err!=nil || lease2==nil { t.Fatalf("second lease=%v err=%v",lease2,err) }
	_ = store.StartLease(lease2.Token)
	updated,err=store.CompleteLease(lease2.Token,CompleteRequest{ExitCode:7,Error:"still broken"})
	if err!=nil { t.Fatal(err) }
	if updated.Runtime["a"].State!=TaskFailed { t.Fatalf("a=%s",updated.Runtime["a"].State) }
	if updated.State!=WorkflowFailed { t.Fatalf("workflow=%s",updated.State) }
	if updated.Runtime["b"].State!=TaskCancelled { t.Fatalf("dependent b=%s",updated.Runtime["b"].State) }
	if updated.ID!=w.ID { t.Fatal("workflow identity changed") }
}

func TestExpiredLeaseCanBeRecovered(t *testing.T) {
	store,clock:=testStore(t)
	_,_,err:=store.CreateWorkflow(simpleSpec(),"")
	if err!=nil { t.Fatal(err) }
	worker:=store.RegisterWorker(RegisterWorkerRequest{Name:"w",Version:"1",Capacity:1})
	lease,_:=store.LeaseNext(worker.ID)
	if lease==nil { t.Fatal("missing lease") }
	_ = store.StartLease(lease.Token)
	clock.Advance(11*time.Second)
	store.Sweep()
	workers:=store.ListWorkers()
	if workers[0].ActiveLeases!=0 { t.Fatalf("active leases=%d",workers[0].ActiveLeases) }
	replacement,err:=store.LeaseNext(worker.ID)
	if err!=nil || replacement==nil { t.Fatalf("replacement=%v err=%v",replacement,err) }
	if replacement.TaskID!=lease.TaskID { t.Fatalf("replacement task=%s",replacement.TaskID) }
	if replacement.Token==lease.Token { t.Fatal("lease token was reused") }
	if _,err:=store.CompleteLease(lease.Token,CompleteRequest{ExitCode:0}); err==nil { t.Fatal("stale completion unexpectedly succeeded") }
}

func TestWorkerCapacityIsEnforced(t *testing.T) {
	store,_:=testStore(t)
	_,_,err:=store.CreateWorkflow(WorkflowSpec{Name:"parallel",Tasks:[]TaskSpec{
		{ID:"a",Command:[]string{"true"}}, {ID:"b",Command:[]string{"true"}}, {ID:"c",Command:[]string{"true"}},
	}},"")
	if err!=nil { t.Fatal(err) }
	worker:=store.RegisterWorker(RegisterWorkerRequest{Name:"w",Version:"1",Capacity:2})
	one,_:=store.LeaseNext(worker.ID)
	two,_:=store.LeaseNext(worker.ID)
	three,_:=store.LeaseNext(worker.ID)
	if one==nil || two==nil { t.Fatal("expected first two leases") }
	if three!=nil { t.Fatal("capacity=2 worker received third lease") }
	if _,err:=store.CompleteLease(one.Token,CompleteRequest{ExitCode:0}); err!=nil { t.Fatal(err) }
	three,_=store.LeaseNext(worker.ID)
	if three==nil { t.Fatal("slot was not released") }
}

func TestTaskLabelPlacement(t *testing.T) {
	store,_:=testStore(t)
	_,_,err:=store.CreateWorkflow(WorkflowSpec{Name:"placement",Tasks:[]TaskSpec{
		{ID:"gpu",Command:[]string{"true"},Labels:map[string]string{"accelerator":"gpu"}},
	}},"")
	if err!=nil { t.Fatal(err) }
	cpu:=store.RegisterWorker(RegisterWorkerRequest{Name:"cpu",Version:"1",Capacity:1,Labels:map[string]string{"accelerator":"cpu"}})
	gpu:=store.RegisterWorker(RegisterWorkerRequest{Name:"gpu",Version:"1",Capacity:1,Labels:map[string]string{"accelerator":"gpu"}})
	if lease,_:=store.LeaseNext(cpu.ID); lease!=nil { t.Fatal("CPU worker received GPU task") }
	lease,err:=store.LeaseNext(gpu.ID)
	if err!=nil || lease==nil || lease.TaskID!="gpu" { t.Fatalf("gpu lease=%v err=%v",lease,err) }
}

func TestRenewExtendsLease(t *testing.T) {
	store,clock:=testStore(t)
	_,_,_ = store.CreateWorkflow(simpleSpec(),"")
	worker:=store.RegisterWorker(RegisterWorkerRequest{Name:"w",Version:"1",Capacity:1})
	lease,_:=store.LeaseNext(worker.ID)
	original:=lease.ExpiresAt
	clock.Advance(5*time.Second)
	renewed,err:=store.RenewLease(lease.Token)
	if err!=nil { t.Fatal(err) }
	if !renewed.ExpiresAt.After(original) { t.Fatalf("renewed=%s original=%s",renewed.ExpiresAt,original) }
}

func TestAppendLogTracksOutputLimitAccounting(t *testing.T) {
	store,_:=testStore(t)
	w,_,_:=store.CreateWorkflow(simpleSpec(),"")
	worker:=store.RegisterWorker(RegisterWorkerRequest{Name:"w",Version:"1",Capacity:1})
	lease,_:=store.LeaseNext(worker.ID)
	if err:=store.AppendLog(lease.Token,"stdout","hello\n");err!=nil { t.Fatal(err) }
	snapshot,ok:=store.GetWorkflow(w.ID)
	if !ok { t.Fatal("missing workflow") }
	if got:=snapshot.Runtime["a"].OutputBytes; got!=6 { t.Fatalf("output bytes=%d",got) }
}


func TestCancelWorkflowReleasesWorkerSlotsAndLeaseMetadata(t *testing.T) {
	store, _ := testStore(t)
	workflow, _, err := store.CreateWorkflow(WorkflowSpec{Name: "cancel-active", Tasks: []TaskSpec{
		{ID: "a", Command: []string{"true"}},
		{ID: "b", Command: []string{"true"}},
	}}, "")
	if err != nil {
		t.Fatal(err)
	}
	worker := store.RegisterWorker(RegisterWorkerRequest{Name: "w", Version: "1", Capacity: 2})
	lease, err := store.LeaseNext(worker.ID)
	if err != nil || lease == nil {
		t.Fatalf("lease=%v err=%v", lease, err)
	}
	if err := store.StartLease(lease.Token); err != nil {
		t.Fatal(err)
	}

	cancelled, err := store.CancelWorkflow(workflow.ID)
	if err != nil {
		t.Fatal(err)
	}
	if cancelled.State != WorkflowCancelled {
		t.Fatalf("state=%s", cancelled.State)
	}
	rt := cancelled.Runtime[lease.TaskID]
	if rt.LeaseToken != "" || rt.LeaseOwner != "" || rt.LeaseUntil != nil {
		t.Fatalf("cancelled task retained lease metadata: %+v", rt)
	}
	workers := store.ListWorkers()
	if len(workers) != 1 || workers[0].ActiveLeases != 0 {
		t.Fatalf("worker active leases after cancel: %+v", workers)
	}
	if _, ok := store.leases[lease.Token]; ok {
		t.Fatal("cancelled lease remained in store")
	}
}

func TestAppendLogLimitFailureDoesNotCorruptAccounting(t *testing.T) {
	store, _ := testStore(t)
	workflow, _, err := store.CreateWorkflow(simpleSpec(), "")
	if err != nil {
		t.Fatal(err)
	}
	worker := store.RegisterWorker(RegisterWorkerRequest{Name: "w", Version: "1", Capacity: 1})
	lease, err := store.LeaseNext(worker.ID)
	if err != nil || lease == nil {
		t.Fatalf("lease=%v err=%v", lease, err)
	}

	store.workflows[workflow.ID].Runtime[lease.TaskID].OutputBytes = 32*1024*1024 - 1
	if err := store.AppendLog(lease.Token, "stdout", "xx"); err == nil {
		t.Fatal("expected output limit error")
	}
	snapshot, _ := store.GetWorkflow(workflow.ID)
	if got := snapshot.Runtime[lease.TaskID].OutputBytes; got != 32*1024*1024-1 {
		t.Fatalf("rejected log changed accounting: %d", got)
	}
}

func TestLeaseExpiresAtExactBoundary(t *testing.T) {
	store, clock := testStore(t)
	_, _, err := store.CreateWorkflow(simpleSpec(), "")
	if err != nil {
		t.Fatal(err)
	}
	worker := store.RegisterWorker(RegisterWorkerRequest{Name: "w", Version: "1", Capacity: 1})
	lease, err := store.LeaseNext(worker.ID)
	if err != nil || lease == nil {
		t.Fatalf("lease=%v err=%v", lease, err)
	}
	clock.Advance(store.leaseDuration)
	if err := store.StartLease(lease.Token); err == nil {
		t.Fatal("lease was accepted exactly at expiration boundary")
	}
	if got := store.ListWorkers()[0].ActiveLeases; got != 0 {
		t.Fatalf("expired lease did not release worker slot: %d", got)
	}
}

func TestSnapshotsAndLeaseSpecsAreDeepCopies(t *testing.T) {
	store, _ := testStore(t)
	spec := WorkflowSpec{Name: "copies", Tasks: []TaskSpec{{
		ID: "a",
		Command: []string{"sh", "-lc", "true"},
		Env: map[string]string{"A": "original"},
		Labels: map[string]string{"os": "linux"},
	}}}
	workflow, _, err := store.CreateWorkflow(spec, "")
	if err != nil {
		t.Fatal(err)
	}
	worker := store.RegisterWorker(RegisterWorkerRequest{
		Name: "w", Version: "1", Capacity: 1, Labels: map[string]string{"os": "linux"},
	})
	lease, err := store.LeaseNext(worker.ID)
	if err != nil || lease == nil {
		t.Fatalf("lease=%v err=%v", lease, err)
	}

	lease.Spec.Env["A"] = "mutated-through-lease"
	first, _ := store.GetWorkflow(workflow.ID)
	first.Tasks["a"].Env["A"] = "mutated-through-snapshot"
	if first.Runtime["a"].LeaseUntil == nil {
		t.Fatal("expected lease timestamp")
	}
	*first.Runtime["a"].LeaseUntil = time.Unix(1, 0).UTC()

	second, _ := store.GetWorkflow(workflow.ID)
	if got := second.Tasks["a"].Env["A"]; got != "original" {
		t.Fatalf("internal task env was mutated: %q", got)
	}
	if second.Runtime["a"].LeaseUntil == nil || second.Runtime["a"].LeaseUntil.Equal(time.Unix(1, 0).UTC()) {
		t.Fatal("internal lease timestamp was mutated through snapshot")
	}
}
