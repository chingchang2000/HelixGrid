package core

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

type Lease struct {
	Token      string    `json:"token"`
	WorkflowID string    `json:"workflow_id"`
	TaskID     string    `json:"task_id"`
	WorkerID   string    `json:"worker_id"`
	Attempt    int       `json:"attempt"`
	ExpiresAt  time.Time `json:"expires_at"`
	Spec       TaskSpec  `json:"spec"`
}

type CompleteRequest struct {
	ExitCode int    `json:"exit_code"`
	Error    string `json:"error,omitempty"`
}

type RegisterWorkerRequest struct {
	Name     string            `json:"name"`
	Version  string            `json:"version"`
	Labels   map[string]string `json:"labels,omitempty"`
	Capacity int               `json:"capacity"`
}

type Store struct {
	mu             sync.RWMutex
	workflows      map[string]*Workflow
	workers        map[string]*Worker
	leases         map[string]Lease
	idempotency    map[string]string
	events         *EventBus
	leaseDuration  time.Duration
	workerTTL      time.Duration
	clock          func() time.Time
}

func NewStore(events *EventBus) *Store {
	if events == nil { events = NewEventBus(50_000) }
	return &Store{
		workflows: map[string]*Workflow{}, workers: map[string]*Worker{}, leases: map[string]Lease{},
		idempotency: map[string]string{}, events: events, leaseDuration: 20 * time.Second,
		workerTTL: 45 * time.Second, clock: func() time.Time { return time.Now().UTC() },
	}
}

func (s *Store) Events() *EventBus { return s.events }

func newID(prefix string) string {
	buf := make([]byte, 12)
	if _, err := rand.Read(buf); err != nil {
		panic("crypto/rand failed: " + err.Error())
	}
	return prefix + "_" + hex.EncodeToString(buf)
}

func (s *Store) CreateWorkflow(spec WorkflowSpec, idempotencyKey string) (*Workflow, bool, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	if idempotencyKey != "" {
		if id, ok := s.idempotency[idempotencyKey]; ok {
			return cloneWorkflow(s.workflows[id]), false, nil
		}
	}
	now := s.clock()
	w, err := NewWorkflow(newID("wf"), spec, now)
	if err != nil { return nil, false, err }
	w.Recompute(now)
	s.workflows[w.ID] = w
	if idempotencyKey != "" { s.idempotency[idempotencyKey] = w.ID }
	s.events.Publish(Event{Type: EventWorkflowCreated, WorkflowID: w.ID, At: now, Data: map[string]string{"name": w.Name}})
	if w.State == WorkflowRunning { s.events.Publish(Event{Type: EventWorkflowStarted, WorkflowID: w.ID, At: now}) }
	for _, id := range w.Order {
		if w.Runtime[id].State == TaskReady { s.events.Publish(Event{Type: EventTaskReady, WorkflowID: w.ID, TaskID: id, At: now}) }
	}
	return cloneWorkflow(w), true, nil
}

func (s *Store) GetWorkflow(id string) (*Workflow, bool) {
	s.mu.RLock(); defer s.mu.RUnlock()
	w, ok := s.workflows[id]
	return cloneWorkflow(w), ok
}

func (s *Store) ListWorkflows() []*Workflow {
	s.mu.RLock(); defer s.mu.RUnlock()
	out := make([]*Workflow, 0, len(s.workflows))
	for _, w := range s.workflows { out = append(out, cloneWorkflow(w)) }
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	return out
}

func (s *Store) CancelWorkflow(id string) (*Workflow, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	w, ok := s.workflows[id]
	if !ok {
		return nil, errors.New("workflow not found")
	}
	before := w.State
	now := s.clock()

	cancelledTasks := make([]string, 0)
	if !isTerminalWorkflowState(w.State) {
		for _, taskID := range w.Order {
			state := w.Runtime[taskID].State
			if state != TaskSucceeded && state != TaskFailed && state != TaskCancelled {
				cancelledTasks = append(cancelledTasks, taskID)
			}
		}
	}

	w.Cancel(now)
	if before != w.State {
		for token, lease := range s.leases {
			if lease.WorkflowID != id {
				continue
			}
			delete(s.leases, token)
			if worker := s.workers[lease.WorkerID]; worker != nil && worker.ActiveLeases > 0 {
				worker.ActiveLeases--
			}
			if rt := w.Runtime[lease.TaskID]; rt != nil && rt.LeaseToken == token {
				rt.LeaseToken = ""
				rt.LeaseOwner = ""
				rt.LeaseUntil = nil
			}
		}
		for _, taskID := range cancelledTasks {
			s.events.Publish(Event{Type: EventTaskCancelled, WorkflowID: id, TaskID: taskID, At: now})
		}
		s.events.Publish(Event{Type: EventWorkflowCancelled, WorkflowID: id, At: now})
	}
	return cloneWorkflow(w), nil
}

func (s *Store) RegisterWorker(req RegisterWorkerRequest) *Worker {
	s.mu.Lock(); defer s.mu.Unlock()
	if req.Capacity <= 0 { req.Capacity = 1 }
	if req.Capacity > 256 { req.Capacity = 256 }
	now := s.clock()
	w := &Worker{ID: newID("worker"), Name: req.Name, Version: req.Version, Labels: cloneMap(req.Labels), Capacity: req.Capacity, RegisteredAt: now, LastHeartbeat: now}
	s.workers[w.ID] = w
	s.events.Publish(Event{Type: EventWorkerRegistered, WorkerID: w.ID, At: now, Data: map[string]string{"name": w.Name}})
	return cloneWorker(w)
}

func (s *Store) HeartbeatWorker(id string) (*Worker, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	w, ok := s.workers[id]
	if !ok { return nil, errors.New("worker not found") }
	w.LastHeartbeat = s.clock()
	s.events.Publish(Event{Type: EventWorkerHeartbeat, WorkerID: id, At: w.LastHeartbeat})
	return cloneWorker(w), nil
}

func (s *Store) ListWorkers() []*Worker {
	s.mu.RLock(); defer s.mu.RUnlock()
	out := make([]*Worker, 0, len(s.workers))
	for _, w := range s.workers { out = append(out, cloneWorker(w)) }
	sort.Slice(out, func(i, j int) bool { return out[i].RegisteredAt.Before(out[j].RegisteredAt) })
	return out
}

func labelsMatch(worker, task map[string]string) bool {
	for k, v := range task { if worker[k] != v { return false } }
	return true
}

func (s *Store) LeaseNext(workerID string) (*Lease, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	now := s.clock()
	s.sweepLocked(now)
	worker, ok := s.workers[workerID]
	if !ok { return nil, errors.New("worker not found") }
	if now.Sub(worker.LastHeartbeat) > s.workerTTL { return nil, errors.New("worker heartbeat expired") }
	if worker.ActiveLeases >= worker.Capacity { return nil, nil }

	workflows := make([]*Workflow, 0, len(s.workflows))
	for _, w := range s.workflows {
		if w.State == WorkflowPending || w.State == WorkflowRunning { workflows = append(workflows, w) }
	}
	sort.Slice(workflows, func(i, j int) bool { return workflows[i].CreatedAt.Before(workflows[j].CreatedAt) })
	for _, w := range workflows {
		w.Recompute(now)
		for _, taskID := range w.Order {
			rt := w.Runtime[taskID]
			spec := w.Tasks[taskID]
			if rt.State != TaskReady || !labelsMatch(worker.Labels, spec.Labels) { continue }
			token := newID("lease")
			expires := now.Add(s.leaseDuration)
			if err := TransitionTask(rt, TaskLeased); err != nil { return nil, err }
			rt.Attempt++
			rt.LeaseToken, rt.LeaseOwner, rt.LeaseUntil = token, workerID, &expires
			worker.ActiveLeases++
			lease := Lease{
				Token: token, WorkflowID: w.ID, TaskID: taskID, WorkerID: workerID,
				Attempt: rt.Attempt, ExpiresAt: expires, Spec: cloneTaskSpec(spec),
			}
			s.leases[token] = lease
			s.events.Publish(Event{Type: EventTaskLeased, WorkflowID: w.ID, TaskID: taskID, WorkerID: workerID, At: now, Data: map[string]string{"attempt": fmt.Sprint(rt.Attempt)}})
			copy := lease
			return &copy, nil
		}
	}
	return nil, nil
}

func (s *Store) StartLease(token string) error {
	s.mu.Lock(); defer s.mu.Unlock()
	lease, ok := s.leases[token]
	if !ok { return errors.New("lease not found") }
	now := s.clock()
	if !now.Before(lease.ExpiresAt) {
		s.expireLeaseLocked(token, lease, now)
		return errors.New("lease expired")
	}
	w := s.workflows[lease.WorkflowID]
	rt := w.Runtime[lease.TaskID]
	if rt.LeaseToken != token { return errors.New("stale lease") }
	if rt.State == TaskLeased {
		if err := TransitionTask(rt, TaskRunning); err != nil { return err }
		rt.StartedAt = ptrTime(now)
		s.events.Publish(Event{Type: EventTaskStarted, WorkflowID: w.ID, TaskID: lease.TaskID, WorkerID: lease.WorkerID, At: now})
	}
	return nil
}

func (s *Store) RenewLease(token string) (*Lease, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	lease, ok := s.leases[token]
	if !ok { return nil, errors.New("lease not found") }
	now := s.clock()
	if !now.Before(lease.ExpiresAt) {
		s.expireLeaseLocked(token, lease, now)
		return nil, errors.New("lease expired")
	}
	lease.ExpiresAt = now.Add(s.leaseDuration)
	s.leases[token] = lease
	if w := s.workflows[lease.WorkflowID]; w != nil {
		if rt := w.Runtime[lease.TaskID]; rt != nil { rt.LeaseUntil = ptrTime(lease.ExpiresAt) }
	}
	copy := lease
	copy.Spec = cloneTaskSpec(lease.Spec)
	return &copy, nil
}

func (s *Store) AppendLog(token, stream, text string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	lease, ok := s.leases[token]
	if !ok {
		return errors.New("lease not found")
	}
	now := s.clock()
	if !now.Before(lease.ExpiresAt) {
		s.expireLeaseLocked(token, lease, now)
		return errors.New("lease expired")
	}
	w := s.workflows[lease.WorkflowID]
	rt := w.Runtime[lease.TaskID]
	if rt.LeaseToken != token {
		return errors.New("stale lease")
	}
	if stream != "stdout" && stream != "stderr" {
		return errors.New("stream must be stdout or stderr")
	}
	size := int64(len(text))
	if size > 64*1024 {
		return errors.New("log chunk too large")
	}
	if rt.OutputBytes > 32*1024*1024-size {
		return errors.New("task output limit exceeded")
	}
	rt.OutputBytes += size
	s.events.Publish(Event{Type: EventTaskLog, WorkflowID: w.ID, TaskID: lease.TaskID, WorkerID: lease.WorkerID, At: now, Data: map[string]string{"stream": stream, "text": text}})
	return nil
}

func (s *Store) CompleteLease(token string, req CompleteRequest) (*Workflow, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	lease, ok := s.leases[token]
	if !ok { return nil, errors.New("lease not found") }
	now := s.clock()
	if !now.Before(lease.ExpiresAt) {
		s.expireLeaseLocked(token, lease, now)
		return nil, errors.New("lease expired")
	}
	w := s.workflows[lease.WorkflowID]
	rt := w.Runtime[lease.TaskID]
	if rt.LeaseToken != token { return nil, errors.New("stale lease") }
	if rt.State == TaskLeased { _ = TransitionTask(rt, TaskRunning); rt.StartedAt = ptrTime(now) }
	if rt.State != TaskRunning { return nil, fmt.Errorf("task is %s, not running", rt.State) }
	rt.ExitCode = &req.ExitCode
	rt.Error = req.Error
	rt.FinishedAt = ptrTime(now)
	delete(s.leases, token)
	if worker := s.workers[lease.WorkerID]; worker != nil && worker.ActiveLeases > 0 { worker.ActiveLeases-- }
	rt.LeaseToken, rt.LeaseOwner, rt.LeaseUntil = "", "", nil

	if req.ExitCode == 0 && req.Error == "" {
		_ = TransitionTask(rt, TaskSucceeded)
		s.events.Publish(Event{Type: EventTaskSucceeded, WorkflowID: w.ID, TaskID: lease.TaskID, WorkerID: lease.WorkerID, At: now})
	} else {
		policy := NormalizeRetry(w.Tasks[lease.TaskID].Retry)
		if rt.Attempt < policy.MaxAttempts {
			_ = TransitionTask(rt, TaskRetryWait)
			next := now.Add(RetryDelay(policy, rt.Attempt))
			rt.NextRetryAt = &next
			s.events.Publish(Event{Type: EventTaskRetry, WorkflowID: w.ID, TaskID: lease.TaskID, WorkerID: lease.WorkerID, At: now, Data: map[string]string{"next_retry_at": next.Format(time.RFC3339Nano)}})
		} else {
			_ = TransitionTask(rt, TaskFailed)
			s.events.Publish(Event{Type: EventTaskFailed, WorkflowID: w.ID, TaskID: lease.TaskID, WorkerID: lease.WorkerID, At: now, Data: map[string]string{"error": req.Error, "exit_code": fmt.Sprint(req.ExitCode)}})
		}
	}
	before := w.State
	w.Recompute(now)
	s.emitWorkflowTerminalLocked(w, before, now)
	for _, id := range w.Order {
		if w.Runtime[id].State == TaskReady {
			// Repeated task.ready events are harmless and useful to wake pollers.
			s.events.Publish(Event{Type: EventTaskReady, WorkflowID: w.ID, TaskID: id, At: now})
		}
	}
	return cloneWorkflow(w), nil
}

func (s *Store) Sweep() {
	s.mu.Lock(); defer s.mu.Unlock()
	s.sweepLocked(s.clock())
}

func (s *Store) sweepLocked(now time.Time) {
	for token, lease := range s.leases {
		if !now.Before(lease.ExpiresAt) { s.expireLeaseLocked(token, lease, now) }
	}
	for _, w := range s.workflows {
		before := w.State
		w.Recompute(now)
		s.emitWorkflowTerminalLocked(w, before, now)
	}
}

func (s *Store) expireLeaseLocked(token string, lease Lease, now time.Time) {
	delete(s.leases, token)
	if worker := s.workers[lease.WorkerID]; worker != nil && worker.ActiveLeases > 0 { worker.ActiveLeases-- }
	w := s.workflows[lease.WorkflowID]
	if w == nil { return }
	rt := w.Runtime[lease.TaskID]
	if rt == nil || rt.LeaseToken != token { return }
	rt.LeaseToken, rt.LeaseOwner, rt.LeaseUntil = "", "", nil
	if rt.State == TaskLeased || rt.State == TaskRunning {
		rt.State = TaskReady
		rt.StartedAt = nil
		s.events.Publish(Event{Type: EventLeaseExpired, WorkflowID: w.ID, TaskID: lease.TaskID, WorkerID: lease.WorkerID, At: now})
	}
}

func isTerminalWorkflowState(state WorkflowState) bool {
	return state == WorkflowSucceeded || state == WorkflowFailed || state == WorkflowCancelled
}

func (s *Store) emitWorkflowTerminalLocked(w *Workflow, before WorkflowState, now time.Time) {
	if w.State == before { return }
	switch w.State {
	case WorkflowSucceeded: s.events.Publish(Event{Type: EventWorkflowSucceeded, WorkflowID: w.ID, At: now})
	case WorkflowFailed: s.events.Publish(Event{Type: EventWorkflowFailed, WorkflowID: w.ID, At: now})
	case WorkflowCancelled: s.events.Publish(Event{Type: EventWorkflowCancelled, WorkflowID: w.ID, At: now})
	}
}

func cloneWorker(w *Worker) *Worker {
	if w == nil { return nil }
	copy := *w; copy.Labels = cloneMap(w.Labels); return &copy
}

func cloneTaskSpec(spec TaskSpec) TaskSpec {
	spec.Command = append([]string(nil), spec.Command...)
	spec.DependsOn = append([]string(nil), spec.DependsOn...)
	spec.Env = cloneMap(spec.Env)
	spec.Labels = cloneMap(spec.Labels)
	return spec
}

func cloneTimePointer(value *time.Time) *time.Time {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func cloneIntPointer(value *int) *int {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func cloneWorkflow(w *Workflow) *Workflow {
	if w == nil {
		return nil
	}
	out := *w
	out.Metadata = cloneMap(w.Metadata)
	out.Order = append([]string(nil), w.Order...)
	out.FinishedAt = cloneTimePointer(w.FinishedAt)
	out.Tasks = make(map[string]TaskSpec, len(w.Tasks))
	out.Runtime = make(map[string]*TaskRuntime, len(w.Runtime))
	for id, spec := range w.Tasks {
		out.Tasks[id] = cloneTaskSpec(spec)
	}
	for id, rt := range w.Runtime {
		copy := *rt
		copy.LeaseUntil = cloneTimePointer(rt.LeaseUntil)
		copy.StartedAt = cloneTimePointer(rt.StartedAt)
		copy.FinishedAt = cloneTimePointer(rt.FinishedAt)
		copy.NextRetryAt = cloneTimePointer(rt.NextRetryAt)
		copy.ExitCode = cloneIntPointer(rt.ExitCode)
		out.Runtime[id] = &copy
	}
	return &out
}
