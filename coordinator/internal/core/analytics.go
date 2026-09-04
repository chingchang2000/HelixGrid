package core

import (
	"cmp"
	"errors"
	"fmt"
	"math"
	"sort"
	"time"
)

// GraphAnalysis contains deterministic structural information derived from a workflow DAG.
// It is independent of runtime state and can therefore be used before a workflow is scheduled.
type GraphAnalysis struct {
	TaskCount          int                `json:"task_count"`
	EdgeCount          int                `json:"edge_count"`
	Roots              []string           `json:"roots"`
	Sinks              []string           `json:"sinks"`
	TopologicalOrder   []string           `json:"topological_order"`
	Levels             [][]string         `json:"levels"`
	DepthByTask        map[string]int     `json:"depth_by_task"`
	DescendantsByTask  map[string]int     `json:"descendants_by_task"`
	CriticalPath       []string           `json:"critical_path"`
	CriticalPathWeight int64              `json:"critical_path_weight"`
	MaxParallelism     int                `json:"max_parallelism"`
	MaxInDegree        int                `json:"max_in_degree"`
	MaxOutDegree       int                `json:"max_out_degree"`
	Bottlenecks        []Bottleneck       `json:"bottlenecks"`
}

// Bottleneck ranks tasks by structural influence. Score is deterministic and combines
// descendants, fan-out and distance from a terminal node. It is intentionally heuristic:
// the purpose is to highlight tasks worth investigating rather than predict exact latency.
type Bottleneck struct {
	TaskID      string  `json:"task_id"`
	Score       float64 `json:"score"`
	Descendants int     `json:"descendants"`
	OutDegree   int     `json:"out_degree"`
	Depth       int     `json:"depth"`
}

// RuntimeAnalysis is a snapshot of dynamic workflow execution state.
type RuntimeAnalysis struct {
	WorkflowID       string             `json:"workflow_id"`
	WorkflowState    WorkflowState      `json:"workflow_state"`
	TaskStates       map[TaskState]int  `json:"task_states"`
	Attempts         int                `json:"attempts"`
	Retries          int                `json:"retries"`
	OutputBytes      int64              `json:"output_bytes"`
	ReadyTasks       []string           `json:"ready_tasks"`
	ActiveTasks      []string           `json:"active_tasks"`
	BlockedTasks     []BlockedTask      `json:"blocked_tasks"`
	LongestTask      *TaskTiming        `json:"longest_task,omitempty"`
	Elapsed          time.Duration      `json:"elapsed"`
	CompletionRatio  float64            `json:"completion_ratio"`
	AttemptExpansion float64            `json:"attempt_expansion"`
}

// BlockedTask explains why a pending task cannot currently run.
type BlockedTask struct {
	TaskID       string   `json:"task_id"`
	WaitingOn    []string `json:"waiting_on"`
	ImpossibleBy []string `json:"impossible_by,omitempty"`
}

// TaskTiming contains an observed task runtime interval.
type TaskTiming struct {
	TaskID   string        `json:"task_id"`
	Duration time.Duration `json:"duration"`
	Attempt  int           `json:"attempt"`
}

// AnalyzeGraph computes deterministic DAG metrics. weights may contain arbitrary positive
// costs (for example estimated milliseconds). Missing or non-positive weights default to 1.
func AnalyzeGraph(spec WorkflowSpec, weights map[string]int64) (GraphAnalysis, error) {
	order, err := ValidateSpec(spec)
	if err != nil {
		return GraphAnalysis{}, err
	}
	byID := make(map[string]TaskSpec, len(spec.Tasks))
	children := make(map[string][]string, len(spec.Tasks))
	indegree := make(map[string]int, len(spec.Tasks))
	outdegree := make(map[string]int, len(spec.Tasks))
	edges := 0
	for _, task := range spec.Tasks {
		byID[task.ID] = task
		children[task.ID] = nil
		indegree[task.ID] = len(task.DependsOn)
		edges += len(task.DependsOn)
	}
	for _, task := range spec.Tasks {
		for _, dependency := range task.DependsOn {
			children[dependency] = append(children[dependency], task.ID)
			outdegree[dependency]++
		}
	}
	for id := range children {
		sort.Strings(children[id])
	}

	roots := make([]string, 0)
	sinks := make([]string, 0)
	maxIn, maxOut := 0, 0
	for _, id := range order {
		if indegree[id] == 0 {
			roots = append(roots, id)
		}
		if outdegree[id] == 0 {
			sinks = append(sinks, id)
		}
		maxIn = max(maxIn, indegree[id])
		maxOut = max(maxOut, outdegree[id])
	}

	depth := make(map[string]int, len(order))
	levels := make([][]string, 0)
	for _, id := range order {
		level := 0
		for _, dependency := range byID[id].DependsOn {
			level = max(level, depth[dependency]+1)
		}
		depth[id] = level
		for len(levels) <= level {
			levels = append(levels, nil)
		}
		levels[level] = append(levels[level], id)
	}
	maxParallelism := 0
	for _, level := range levels {
		sort.Strings(level)
		maxParallelism = max(maxParallelism, len(level))
	}

	descendants := descendantsCount(order, children)
	criticalPath, criticalWeight := longestPath(order, byID, weights)
	bottlenecks := rankBottlenecks(order, depth, descendants, outdegree)

	return GraphAnalysis{
		TaskCount:          len(spec.Tasks),
		EdgeCount:          edges,
		Roots:              roots,
		Sinks:              sinks,
		TopologicalOrder:   append([]string(nil), order...),
		Levels:             levels,
		DepthByTask:        depth,
		DescendantsByTask:  descendants,
		CriticalPath:       criticalPath,
		CriticalPathWeight: criticalWeight,
		MaxParallelism:     maxParallelism,
		MaxInDegree:        maxIn,
		MaxOutDegree:       maxOut,
		Bottlenecks:        bottlenecks,
	}, nil
}

func descendantsCount(order []string, children map[string][]string) map[string]int {
	sets := make(map[string]map[string]struct{}, len(order))
	for i := len(order) - 1; i >= 0; i-- {
		id := order[i]
		set := make(map[string]struct{})
		for _, child := range children[id] {
			set[child] = struct{}{}
			for descendant := range sets[child] {
				set[descendant] = struct{}{}
			}
		}
		sets[id] = set
	}
	result := make(map[string]int, len(order))
	for id, set := range sets {
		result[id] = len(set)
	}
	return result
}

func longestPath(order []string, byID map[string]TaskSpec, weights map[string]int64) ([]string, int64) {
	score := make(map[string]int64, len(order))
	parent := make(map[string]string, len(order))
	for _, id := range order {
		weight := weights[id]
		if weight <= 0 {
			weight = 1
		}
		bestScore := int64(0)
		bestParent := ""
		dependencies := append([]string(nil), byID[id].DependsOn...)
		sort.Strings(dependencies)
		for _, dependency := range dependencies {
			candidate := score[dependency]
			if candidate > bestScore || (candidate == bestScore && bestParent != "" && dependency < bestParent) {
				bestScore = candidate
				bestParent = dependency
			}
		}
		score[id] = bestScore + weight
		parent[id] = bestParent
	}
	end := ""
	best := int64(-1)
	for _, id := range order {
		if score[id] > best || (score[id] == best && (end == "" || id < end)) {
			best = score[id]
			end = id
		}
	}
	path := make([]string, 0)
	for current := end; current != ""; current = parent[current] {
		path = append(path, current)
	}
	for left, right := 0, len(path)-1; left < right; left, right = left+1, right-1 {
		path[left], path[right] = path[right], path[left]
	}
	return path, max(best, 0)
}

func rankBottlenecks(order []string, depth, descendants, outdegree map[string]int) []Bottleneck {
	items := make([]Bottleneck, 0, len(order))
	for _, id := range order {
		// Descendants dominate the score; fan-out and depth provide stable tie-breaking
		// signals without requiring runtime duration estimates.
		score := float64(descendants[id])*3.0 + float64(outdegree[id])*1.5 + 1.0/float64(depth[id]+1)
		items = append(items, Bottleneck{
			TaskID:      id,
			Score:       score,
			Descendants: descendants[id],
			OutDegree:   outdegree[id],
			Depth:       depth[id],
		})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].Score != items[j].Score {
			return items[i].Score > items[j].Score
		}
		return items[i].TaskID < items[j].TaskID
	})
	if len(items) > 10 {
		items = items[:10]
	}
	return items
}

// AnalyzeRuntime derives execution metrics from a workflow snapshot without mutating it.
func AnalyzeRuntime(workflow *Workflow, now time.Time) (RuntimeAnalysis, error) {
	if workflow == nil {
		return RuntimeAnalysis{}, errors.New("nil workflow")
	}
	states := make(map[TaskState]int)
	ready := make([]string, 0)
	active := make([]string, 0)
	blocked := make([]BlockedTask, 0)
	attempts := 0
	retries := 0
	var outputBytes int64
	var longest *TaskTiming
	completed := 0

	for _, id := range workflow.Order {
		runtime := workflow.Runtime[id]
		spec, ok := workflow.Tasks[id]
		if !ok || runtime == nil {
			return RuntimeAnalysis{}, fmt.Errorf("workflow snapshot missing task/runtime for %q", id)
		}
		states[runtime.State]++
		attempts += runtime.Attempt
		if runtime.Attempt > 1 {
			retries += runtime.Attempt - 1
		}
		outputBytes += runtime.OutputBytes
		if runtime.State == TaskSucceeded || runtime.State == TaskFailed || runtime.State == TaskCancelled {
			completed++
		}
		switch runtime.State {
		case TaskReady:
			ready = append(ready, id)
		case TaskLeased, TaskRunning:
			active = append(active, id)
		case TaskPending:
			waiting := make([]string, 0)
			impossible := make([]string, 0)
			for _, dependency := range spec.DependsOn {
				dependencyRuntime := workflow.Runtime[dependency]
				if dependencyRuntime == nil {
					waiting = append(waiting, dependency)
					continue
				}
				if dependencyRuntime.State != TaskSucceeded {
					waiting = append(waiting, dependency)
				}
				if dependencyRuntime.State == TaskFailed || dependencyRuntime.State == TaskCancelled {
					impossible = append(impossible, dependency)
				}
			}
			sort.Strings(waiting)
			sort.Strings(impossible)
			blocked = append(blocked, BlockedTask{TaskID: id, WaitingOn: waiting, ImpossibleBy: impossible})
		}
		if runtime.StartedAt != nil {
			end := now
			if runtime.FinishedAt != nil {
				end = *runtime.FinishedAt
			}
			duration := end.Sub(*runtime.StartedAt)
			if duration < 0 {
				duration = 0
			}
			candidate := TaskTiming{TaskID: id, Duration: duration, Attempt: runtime.Attempt}
			if longest == nil || candidate.Duration > longest.Duration ||
				(candidate.Duration == longest.Duration && candidate.TaskID < longest.TaskID) {
				copy := candidate
				longest = &copy
			}
		}
	}
	sort.Strings(ready)
	sort.Strings(active)
	sort.Slice(blocked, func(i, j int) bool { return blocked[i].TaskID < blocked[j].TaskID })

	elapsed := now.Sub(workflow.CreatedAt)
	if workflow.FinishedAt != nil {
		elapsed = workflow.FinishedAt.Sub(workflow.CreatedAt)
	}
	if elapsed < 0 {
		elapsed = 0
	}
	completionRatio := 0.0
	attemptExpansion := 0.0
	if len(workflow.Order) > 0 {
		completionRatio = float64(completed) / float64(len(workflow.Order))
		attemptExpansion = float64(attempts) / float64(len(workflow.Order))
	}

	return RuntimeAnalysis{
		WorkflowID:       workflow.ID,
		WorkflowState:    workflow.State,
		TaskStates:       states,
		Attempts:         attempts,
		Retries:          retries,
		OutputBytes:      outputBytes,
		ReadyTasks:       ready,
		ActiveTasks:      active,
		BlockedTasks:     blocked,
		LongestTask:      longest,
		Elapsed:          elapsed,
		CompletionRatio:  completionRatio,
		AttemptExpansion: attemptExpansion,
	}, nil
}

// EstimateMakespan computes a lower-bound-ish scheduling estimate using list scheduling.
// It assumes deterministic durations and identical worker slots. Dependencies are respected;
// worker startup/communication overhead is intentionally excluded.
func EstimateMakespan(spec WorkflowSpec, durations map[string]time.Duration, workers int) (time.Duration, error) {
	if workers <= 0 {
		return 0, errors.New("workers must be positive")
	}
	order, err := ValidateSpec(spec)
	if err != nil {
		return 0, err
	}
	byID := make(map[string]TaskSpec, len(spec.Tasks))
	children := make(map[string][]string, len(spec.Tasks))
	remaining := make(map[string]int, len(spec.Tasks))
	for _, task := range spec.Tasks {
		byID[task.ID] = task
		remaining[task.ID] = len(task.DependsOn)
		for _, dep := range task.DependsOn {
			children[dep] = append(children[dep], task.ID)
		}
	}
	for id := range children {
		sort.Strings(children[id])
	}

	type runningTask struct {
		id     string
		finish time.Duration
	}
	ready := make([]string, 0)
	for _, id := range order {
		if remaining[id] == 0 {
			ready = append(ready, id)
		}
	}
	sort.Strings(ready)
	running := make([]runningTask, 0, workers)
	now := time.Duration(0)
	finished := 0

	for finished < len(order) {
		for len(ready) > 0 && len(running) < workers {
			id := ready[0]
			ready = ready[1:]
			duration := durations[id]
			if duration < 0 {
				return 0, fmt.Errorf("negative duration for task %q", id)
			}
			running = append(running, runningTask{id: id, finish: now + duration})
		}
		if len(running) == 0 {
			return 0, errors.New("scheduler made no progress")
		}
		sort.Slice(running, func(i, j int) bool {
			return cmp.Or(cmp.Compare(running[i].finish, running[j].finish), cmp.Compare(running[i].id, running[j].id)) < 0
		})
		now = running[0].finish
		completed := make([]runningTask, 0)
		remainingRunning := running[:0]
		for _, item := range running {
			if item.finish == now {
				completed = append(completed, item)
			} else {
				remainingRunning = append(remainingRunning, item)
			}
		}
		running = remainingRunning
		for _, item := range completed {
			finished++
			for _, child := range children[item.id] {
				remaining[child]--
				if remaining[child] == 0 {
					ready = append(ready, child)
				}
			}
		}
		sort.Strings(ready)
	}
	return now, nil
}

// Saturation computes a bounded [0,1] signal from active leases and total worker capacity.
func Saturation(workers []*Worker) float64 {
	capacity := 0
	active := 0
	for _, worker := range workers {
		if worker == nil || worker.Capacity <= 0 {
			continue
		}
		capacity += worker.Capacity
		active += max(worker.ActiveLeases, 0)
	}
	if capacity == 0 {
		return 0
	}
	return math.Min(1, math.Max(0, float64(active)/float64(capacity)))
}
