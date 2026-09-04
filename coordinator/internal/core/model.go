package core

import (
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

type WorkflowState string
type TaskState string
type EventType string

const (
	maxWorkflowNameChars   = 200
	maxTaskIDChars         = 200
	maxCommandArgs         = 4096
	maxCommandArgChars     = 65536
	maxStringMapKeyChars   = 128
	maxStringMapValueChars = 4096
	maxMetadataProperties  = 4096
	maxEnvProperties       = 4096
	maxLabelProperties     = 128
	maxRetryBaseDelayMS    = 3_600_000
	maxRetryDelayMS        = 86_400_000
)

var taskIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]*$`)

const (
	WorkflowPending   WorkflowState = "PENDING"
	WorkflowRunning   WorkflowState = "RUNNING"
	WorkflowSucceeded WorkflowState = "SUCCEEDED"
	WorkflowFailed    WorkflowState = "FAILED"
	WorkflowCancelled WorkflowState = "CANCELLED"
)

const (
	TaskPending   TaskState = "PENDING"
	TaskReady     TaskState = "READY"
	TaskLeased    TaskState = "LEASED"
	TaskRunning   TaskState = "RUNNING"
	TaskSucceeded TaskState = "SUCCEEDED"
	TaskFailed    TaskState = "FAILED"
	TaskRetryWait TaskState = "RETRY_WAIT"
	TaskCancelled TaskState = "CANCELLED"
)

const (
	EventWorkflowCreated   EventType = "workflow.created"
	EventWorkflowStarted   EventType = "workflow.started"
	EventWorkflowSucceeded EventType = "workflow.succeeded"
	EventWorkflowFailed    EventType = "workflow.failed"
	EventWorkflowCancelled EventType = "workflow.cancelled"
	EventTaskReady          EventType = "task.ready"
	EventTaskLeased         EventType = "task.leased"
	EventTaskStarted        EventType = "task.started"
	EventTaskLog            EventType = "task.log"
	EventTaskSucceeded      EventType = "task.succeeded"
	EventTaskFailed         EventType = "task.failed"
	EventTaskRetry          EventType = "task.retry"
	EventTaskCancelled      EventType = "task.cancelled"
	EventLeaseExpired       EventType = "lease.expired"
	EventWorkerRegistered   EventType = "worker.registered"
	EventWorkerHeartbeat    EventType = "worker.heartbeat"
)

type RetryPolicy struct {
	MaxAttempts int `json:"max_attempts"`
	BaseDelayMS int `json:"base_delay_ms"`
	MaxDelayMS  int `json:"max_delay_ms,omitempty"`
}

type TaskSpec struct {
	ID             string            `json:"id"`
	DependsOn      []string          `json:"depends_on,omitempty"`
	Command        []string          `json:"command"`
	Env            map[string]string `json:"env,omitempty"`
	TimeoutSeconds int               `json:"timeout_seconds,omitempty"`
	Retry          RetryPolicy       `json:"retry,omitempty"`
	Labels         map[string]string `json:"labels,omitempty"`
}

type WorkflowSpec struct {
	Name     string            `json:"name"`
	Metadata map[string]string `json:"metadata,omitempty"`
	Tasks    []TaskSpec        `json:"tasks"`
}

type TaskRuntime struct {
	State       TaskState  `json:"state"`
	Attempt     int        `json:"attempt"`
	LeaseToken  string     `json:"lease_token,omitempty"`
	LeaseOwner  string     `json:"lease_owner,omitempty"`
	LeaseUntil  *time.Time `json:"lease_until,omitempty"`
	StartedAt   *time.Time `json:"started_at,omitempty"`
	FinishedAt  *time.Time `json:"finished_at,omitempty"`
	NextRetryAt *time.Time `json:"next_retry_at,omitempty"`
	ExitCode    *int       `json:"exit_code,omitempty"`
	Error       string     `json:"error,omitempty"`
	OutputBytes int64      `json:"output_bytes"`
}

type Workflow struct {
	ID         string                  `json:"id"`
	Name       string                  `json:"name"`
	Metadata   map[string]string       `json:"metadata,omitempty"`
	State      WorkflowState           `json:"state"`
	CreatedAt  time.Time               `json:"created_at"`
	UpdatedAt  time.Time               `json:"updated_at"`
	FinishedAt *time.Time              `json:"finished_at,omitempty"`
	Tasks      map[string]TaskSpec     `json:"tasks"`
	Runtime    map[string]*TaskRuntime `json:"runtime"`
	Order      []string                `json:"order"`
}

type Event struct {
	ID         int64             `json:"id"`
	Type       EventType         `json:"type"`
	WorkflowID string            `json:"workflow_id,omitempty"`
	TaskID     string            `json:"task_id,omitempty"`
	WorkerID   string            `json:"worker_id,omitempty"`
	At         time.Time         `json:"at"`
	Data       map[string]string `json:"data,omitempty"`
}

type Worker struct {
	ID            string            `json:"id"`
	Name          string            `json:"name"`
	Version       string            `json:"version"`
	Labels        map[string]string `json:"labels,omitempty"`
	Capacity      int               `json:"capacity"`
	ActiveLeases  int               `json:"active_leases"`
	RegisteredAt  time.Time         `json:"registered_at"`
	LastHeartbeat time.Time         `json:"last_heartbeat"`
}

var validTaskTransitions = map[TaskState]map[TaskState]bool{
	TaskPending:   {TaskReady: true, TaskCancelled: true},
	TaskReady:     {TaskLeased: true, TaskCancelled: true},
	TaskLeased:    {TaskRunning: true, TaskReady: true, TaskCancelled: true},
	TaskRunning:   {TaskSucceeded: true, TaskFailed: true, TaskRetryWait: true, TaskCancelled: true},
	TaskRetryWait: {TaskReady: true, TaskCancelled: true},
	TaskFailed:    {},
	TaskSucceeded: {},
	TaskCancelled: {},
}

func TransitionTask(rt *TaskRuntime, next TaskState) error {
	if rt == nil {
		return errors.New("nil task runtime")
	}
	allowed, ok := validTaskTransitions[rt.State]
	if !ok || !allowed[next] {
		return fmt.Errorf("invalid task transition %s -> %s", rt.State, next)
	}
	rt.State = next
	return nil
}

func NormalizeRetry(p RetryPolicy) RetryPolicy {
	if p.MaxAttempts <= 0 {
		p.MaxAttempts = 1
	}
	if p.MaxAttempts > 100 {
		p.MaxAttempts = 100
	}
	if p.BaseDelayMS <= 0 {
		p.BaseDelayMS = 250
	}
	if p.BaseDelayMS > maxRetryBaseDelayMS {
		p.BaseDelayMS = maxRetryBaseDelayMS
	}
	if p.MaxDelayMS <= 0 {
		p.MaxDelayMS = 30_000
	}
	if p.MaxDelayMS > maxRetryDelayMS {
		p.MaxDelayMS = maxRetryDelayMS
	}
	if p.MaxDelayMS < p.BaseDelayMS {
		p.MaxDelayMS = p.BaseDelayMS
	}
	return p
}

func RetryDelay(p RetryPolicy, attempt int) time.Duration {
	p = NormalizeRetry(p)
	if attempt < 1 {
		attempt = 1
	}
	ms := int64(p.BaseDelayMS)
	for i := 1; i < attempt; i++ {
		if ms >= int64(p.MaxDelayMS) {
			ms = int64(p.MaxDelayMS)
			break
		}
		ms *= 2
		if ms > int64(p.MaxDelayMS) {
			ms = int64(p.MaxDelayMS)
		}
	}
	return time.Duration(ms) * time.Millisecond
}

func ValidateSpec(spec WorkflowSpec) ([]string, error) {
	name := strings.TrimSpace(spec.Name)
	if name == "" {
		return nil, errors.New("workflow name is required")
	}
	if utf8.RuneCountInString(spec.Name) > maxWorkflowNameChars {
		return nil, errors.New("workflow name exceeds 200 characters")
	}
	if err := validateStringMap("workflow metadata", spec.Metadata, maxMetadataProperties); err != nil {
		return nil, err
	}
	if len(spec.Tasks) == 0 {
		return nil, errors.New("workflow must contain at least one task")
	}
	if len(spec.Tasks) > 10_000 {
		return nil, errors.New("workflow exceeds task limit")
	}

	byID := make(map[string]TaskSpec, len(spec.Tasks))
	for i := range spec.Tasks {
		t := spec.Tasks[i]
		if strings.TrimSpace(t.ID) == "" {
			return nil, fmt.Errorf("task %d has empty id", i)
		}
		if utf8.RuneCountInString(t.ID) > maxTaskIDChars || !taskIDPattern.MatchString(t.ID) {
			return nil, fmt.Errorf("task %q has invalid id", t.ID)
		}
		if _, exists := byID[t.ID]; exists {
			return nil, fmt.Errorf("duplicate task id %q", t.ID)
		}
		if len(t.Command) == 0 || strings.TrimSpace(t.Command[0]) == "" {
			return nil, fmt.Errorf("task %q has empty command", t.ID)
		}
		if len(t.Command) > maxCommandArgs {
			return nil, fmt.Errorf("task %q exceeds command argument limit", t.ID)
		}
		for _, arg := range t.Command {
			if utf8.RuneCountInString(arg) > maxCommandArgChars {
				return nil, fmt.Errorf("task %q has command argument exceeding 65536 characters", t.ID)
			}
		}
		if len(t.DependsOn) > 10_000 {
			return nil, fmt.Errorf("task %q exceeds dependency limit", t.ID)
		}
		if err := validateStringMap("task "+t.ID+" env", t.Env, maxEnvProperties); err != nil {
			return nil, err
		}
		if err := validateStringMap("task "+t.ID+" labels", t.Labels, maxLabelProperties); err != nil {
			return nil, err
		}
		if t.TimeoutSeconds < 0 || t.TimeoutSeconds > 86_400 {
			return nil, fmt.Errorf("task %q has invalid timeout", t.ID)
		}
		if t.Retry.MaxAttempts < 0 || t.Retry.MaxAttempts > 100 {
			return nil, fmt.Errorf("task %q has invalid max_attempts", t.ID)
		}
		if t.Retry.BaseDelayMS < 0 || t.Retry.BaseDelayMS > maxRetryBaseDelayMS {
			return nil, fmt.Errorf("task %q has invalid base_delay_ms", t.ID)
		}
		if t.Retry.MaxDelayMS < 0 || t.Retry.MaxDelayMS > maxRetryDelayMS {
			return nil, fmt.Errorf("task %q has invalid max_delay_ms", t.ID)
		}
		if t.Retry.BaseDelayMS > 0 && t.Retry.MaxDelayMS > 0 && t.Retry.MaxDelayMS < t.Retry.BaseDelayMS {
			return nil, fmt.Errorf("task %q has max_delay_ms smaller than base_delay_ms", t.ID)
		}
		byID[t.ID] = t
	}

	indegree := make(map[string]int, len(byID))
	children := make(map[string][]string, len(byID))
	for id := range byID {
		indegree[id] = 0
	}
	for _, t := range spec.Tasks {
		seen := map[string]bool{}
		for _, dep := range t.DependsOn {
			if dep == t.ID {
				return nil, fmt.Errorf("task %q depends on itself", t.ID)
			}
			if seen[dep] {
				return nil, fmt.Errorf("task %q repeats dependency %q", t.ID, dep)
			}
			seen[dep] = true
			if _, exists := byID[dep]; !exists {
				return nil, fmt.Errorf("task %q references unknown dependency %q", t.ID, dep)
			}
			indegree[t.ID]++
			children[dep] = append(children[dep], t.ID)
		}
	}

	queue := make([]string, 0)
	for id, d := range indegree {
		if d == 0 {
			queue = append(queue, id)
		}
	}
	sort.Strings(queue)
	order := make([]string, 0, len(byID))
	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:]
		order = append(order, id)
		cs := append([]string(nil), children[id]...)
		sort.Strings(cs)
		for _, child := range cs {
			indegree[child]--
			if indegree[child] == 0 {
				queue = append(queue, child)
				sort.Strings(queue)
			}
		}
	}
	if len(order) != len(byID) {
		return nil, errors.New("workflow graph contains a dependency cycle")
	}
	return order, nil
}

func validateStringMap(name string, values map[string]string, maxProperties int) error {
	if len(values) > maxProperties {
		return fmt.Errorf("%s exceeds property limit", name)
	}
	for key, value := range values {
		if strings.TrimSpace(key) == "" || utf8.RuneCountInString(key) > maxStringMapKeyChars {
			return fmt.Errorf("%s contains invalid key %q", name, key)
		}
		if utf8.RuneCountInString(value) > maxStringMapValueChars {
			return fmt.Errorf("%s value for %q exceeds 4096 characters", name, key)
		}
	}
	return nil
}

func NewWorkflow(id string, spec WorkflowSpec, now time.Time) (*Workflow, error) {
	order, err := ValidateSpec(spec)
	if err != nil {
		return nil, err
	}
	w := &Workflow{
		ID: id, Name: spec.Name, Metadata: cloneMap(spec.Metadata), State: WorkflowPending,
		CreatedAt: now, UpdatedAt: now, Tasks: make(map[string]TaskSpec, len(spec.Tasks)),
		Runtime: make(map[string]*TaskRuntime, len(spec.Tasks)), Order: order,
	}
	for _, t := range spec.Tasks {
		t.Retry = NormalizeRetry(t.Retry)
		t.DependsOn = append([]string(nil), t.DependsOn...)
		t.Command = append([]string(nil), t.Command...)
		t.Env = cloneMap(t.Env)
		t.Labels = cloneMap(t.Labels)
		w.Tasks[t.ID] = t
		w.Runtime[t.ID] = &TaskRuntime{State: TaskPending}
	}
	return w, nil
}

func (w *Workflow) Recompute(now time.Time) {
	if w.State == WorkflowCancelled || w.State == WorkflowSucceeded || w.State == WorkflowFailed {
		return
	}
	allSucceeded := true
	anyActive := false
	terminalFailure := false

	for _, id := range w.Order {
		rt := w.Runtime[id]
		spec := w.Tasks[id]
		switch rt.State {
		case TaskPending:
			depsOK := true
			depsImpossible := false
			for _, dep := range spec.DependsOn {
				d := w.Runtime[dep].State
				if d != TaskSucceeded {
					depsOK = false
				}
				if d == TaskFailed || d == TaskCancelled {
					depsImpossible = true
				}
			}
			if depsImpossible {
				_ = TransitionTask(rt, TaskCancelled)
			} else if depsOK {
				_ = TransitionTask(rt, TaskReady)
			}
		case TaskRetryWait:
			if rt.NextRetryAt == nil || !now.Before(*rt.NextRetryAt) {
				rt.NextRetryAt = nil
				_ = TransitionTask(rt, TaskReady)
			}
		}

		if rt.State != TaskSucceeded {
			allSucceeded = false
		}
		if rt.State == TaskReady || rt.State == TaskLeased || rt.State == TaskRunning || rt.State == TaskRetryWait {
			anyActive = true
		}
		if rt.State == TaskFailed {
			terminalFailure = true
		}
	}

	w.UpdatedAt = now
	if allSucceeded {
		w.State = WorkflowSucceeded
		w.FinishedAt = ptrTime(now)
		return
	}
	if terminalFailure && !anyActive {
		w.State = WorkflowFailed
		w.FinishedAt = ptrTime(now)
		return
	}
	if w.State == WorkflowPending {
		w.State = WorkflowRunning
	}
}

func (w *Workflow) Cancel(now time.Time) {
	if w.State == WorkflowCancelled || w.State == WorkflowSucceeded || w.State == WorkflowFailed {
		return
	}
	w.State = WorkflowCancelled
	w.UpdatedAt = now
	w.FinishedAt = ptrTime(now)
	for _, rt := range w.Runtime {
		if rt.State != TaskSucceeded && rt.State != TaskFailed && rt.State != TaskCancelled {
			rt.State = TaskCancelled
			rt.FinishedAt = ptrTime(now)
		}
	}
}

func cloneMap[K comparable, V any](in map[K]V) map[K]V {
	if in == nil { return nil }
	out := make(map[K]V, len(in))
	for k, v := range in { out[k] = v }
	return out
}

func ptrTime(t time.Time) *time.Time { return &t }

// EventBus is a lightweight in-memory append-only event stream with bounded replay.
type EventBus struct {
	mu      sync.RWMutex
	nextID  int64
	max     int
	events  []Event
	subs    map[int]chan Event
	nextSub int
}

func NewEventBus(max int) *EventBus {
	if max <= 0 { max = 50_000 }
	return &EventBus{max: max, subs: map[int]chan Event{}}
}

func (b *EventBus) Publish(e Event) Event {
	b.mu.Lock()
	b.nextID++
	e.ID = b.nextID
	if e.At.IsZero() { e.At = time.Now().UTC() }
	b.events = append(b.events, e)
	if len(b.events) > b.max {
		copy(b.events, b.events[len(b.events)-b.max:])
		b.events = b.events[:b.max]
	}
	for _, ch := range b.subs {
		select { case ch <- e: default: }
	}
	b.mu.Unlock()
	return e
}

func (b *EventBus) Replay(workflowID string, after int64) []Event {
	b.mu.RLock(); defer b.mu.RUnlock()
	out := make([]Event, 0)
	for _, e := range b.events {
		if e.ID > after && (workflowID == "" || e.WorkflowID == workflowID) {
			out = append(out, e)
		}
	}
	return out
}

func (b *EventBus) Subscribe(buffer int) (int, <-chan Event, func()) {
	id, _, ch, cancel := b.SubscribeReplay("", 0, buffer)
	return id, ch, cancel
}

// SubscribeReplay atomically snapshots replayable events and installs a live
// subscription under the same lock. That removes the replay/subscribe race where
// an event could otherwise be published between the two operations and disappear
// from an SSE client's view.
func (b *EventBus) SubscribeReplay(workflowID string, after int64, buffer int) (int, []Event, <-chan Event, func()) {
	if buffer <= 0 {
		buffer = 128
	}
	b.mu.Lock()
	id := b.nextSub
	b.nextSub++
	ch := make(chan Event, buffer)
	b.subs[id] = ch
	replay := make([]Event, 0)
	for _, e := range b.events {
		if e.ID > after && (workflowID == "" || e.WorkflowID == workflowID) {
			replay = append(replay, e)
		}
	}
	b.mu.Unlock()
	cancel := func() {
		b.mu.Lock()
		if c, ok := b.subs[id]; ok {
			delete(b.subs, id)
			close(c)
		}
		b.mu.Unlock()
	}
	return id, replay, ch, cancel
}
