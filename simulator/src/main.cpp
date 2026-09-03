#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <queue>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

namespace helix {

struct Task {
    std::string id;
    double mean_ms{};
    double jitter{};
    double failure_probability{};
    int max_attempts{1};
    std::vector<std::string> depends_on;
};

struct Workflow {
    std::string name;
    std::vector<Task> tasks;
};

struct Options {
    int workers{4};
    int runs{1000};
    std::uint64_t seed{0x48454c4958475249ULL};
    bool demo{false};
    bool json{false};
    bool dot{false};
    bool verbose{false};
};

enum class State {
    pending,
    ready,
    running,
    succeeded,
    failed,
};

struct AttemptResult {
    bool success{};
    double duration_ms{};
};

struct Runtime {
    State state{State::pending};
    int attempts{};
    double started_at{};
    double finished_at{};
    int worker{-1};
};

struct Running {
    std::string task_id;
    int worker{};
    double finish_at{};
    bool success{};
};

struct RunResult {
    bool success{};
    double makespan_ms{};
    int attempts{};
    int failures{};
    double worker_busy_ms{};
};

struct Summary {
    int runs{};
    int successes{};
    double mean_ms{};
    double stddev_ms{};
    double p50_ms{};
    double p90_ms{};
    double p95_ms{};
    double p99_ms{};
    double min_ms{};
    double max_ms{};
    double mean_attempts{};
    double mean_failures{};
    double mean_utilization{};
};

[[noreturn]] void fail(const std::string& message) {
    throw std::runtime_error(message);
}

int parse_int(std::string_view value, std::string_view flag) {
    int out{};
    const auto* first = value.data();
    const auto* last = first + value.size();
    const auto [ptr, ec] = std::from_chars(first, last, out);
    if (ec != std::errc{} || ptr != last) {
        fail("invalid integer for " + std::string(flag) + ": " + std::string(value));
    }
    return out;
}

std::uint64_t parse_u64(std::string_view value, std::string_view flag) {
    std::uint64_t out{};
    const auto* first = value.data();
    const auto* last = first + value.size();
    const auto [ptr, ec] = std::from_chars(first, last, out);
    if (ec != std::errc{} || ptr != last) {
        fail("invalid integer for " + std::string(flag) + ": " + std::string(value));
    }
    return out;
}

Options parse_args(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg{argv[i]};
        auto require_value = [&](std::string_view flag) -> std::string_view {
            if (i + 1 >= argc) fail("missing value for " + std::string(flag));
            return argv[++i];
        };
        if (arg == "--workers") options.workers = parse_int(require_value(arg), arg);
        else if (arg == "--runs") options.runs = parse_int(require_value(arg), arg);
        else if (arg == "--seed") options.seed = parse_u64(require_value(arg), arg);
        else if (arg == "--demo") options.demo = true;
        else if (arg == "--json") options.json = true;
        else if (arg == "--dot") options.dot = true;
        else if (arg == "--verbose") options.verbose = true;
        else if (arg == "--help" || arg == "-h") {
            std::cout
                << "helix-sim - deterministic Monte Carlo scheduler simulator\n\n"
                << "Usage: helix-sim --demo [options]\n\n"
                << "  --workers N    simulated worker slots (default 4)\n"
                << "  --runs N       Monte Carlo runs (default 1000)\n"
                << "  --seed N       deterministic PRNG seed\n"
                << "  --json         emit machine-readable summary\n"
                << "  --dot          print Graphviz DOT and exit\n"
                << "  --verbose      print first-run scheduling trace\n";
            std::exit(0);
        } else {
            fail("unknown argument: " + std::string(arg));
        }
    }
    if (options.workers < 1 || options.workers > 4096) fail("workers must be between 1 and 4096");
    if (options.runs < 1 || options.runs > 10'000'000) fail("runs must be between 1 and 10000000");
    if (!options.demo) fail("this build currently expects --demo; see source for the embeddable simulator API");
    return options;
}

Workflow demo_workflow() {
    return Workflow{
        "release-pipeline",
        {
            {"checkout", 150.0, 0.12, 0.002, 2, {}},
            {"generate", 420.0, 0.18, 0.010, 2, {"checkout"}},
            {"lint", 850.0, 0.20, 0.015, 2, {"generate"}},
            {"unit-linux", 2800.0, 0.25, 0.035, 3, {"generate"}},
            {"unit-windows", 3600.0, 0.30, 0.045, 3, {"generate"}},
            {"unit-macos", 3200.0, 0.28, 0.040, 3, {"generate"}},
            {"integration-db", 5100.0, 0.35, 0.060, 3, {"generate"}},
            {"integration-api", 4400.0, 0.30, 0.050, 3, {"generate"}},
            {"security-scan", 2100.0, 0.22, 0.020, 2, {"generate"}},
            {"package-linux", 1450.0, 0.15, 0.015, 2, {"lint", "unit-linux", "integration-api"}},
            {"package-windows", 1700.0, 0.18, 0.018, 2, {"lint", "unit-windows", "integration-api"}},
            {"package-macos", 1600.0, 0.16, 0.018, 2, {"lint", "unit-macos", "integration-api"}},
            {"migration-check", 900.0, 0.12, 0.010, 2, {"integration-db"}},
            {"sbom", 700.0, 0.10, 0.005, 2, {"security-scan"}},
            {"sign-linux", 350.0, 0.08, 0.010, 2, {"package-linux", "sbom"}},
            {"sign-windows", 400.0, 0.08, 0.010, 2, {"package-windows", "sbom"}},
            {"sign-macos", 380.0, 0.08, 0.010, 2, {"package-macos", "sbom"}},
            {"manifest", 240.0, 0.06, 0.002, 2, {"sign-linux", "sign-windows", "sign-macos", "migration-check"}},
            {"publish", 1250.0, 0.16, 0.030, 4, {"manifest"}},
            {"smoke", 1800.0, 0.20, 0.025, 3, {"publish"}},
            {"finalize", 180.0, 0.05, 0.001, 2, {"smoke"}},
        }
    };
}

class Graph {
public:
    explicit Graph(const Workflow& workflow) : workflow_(workflow) {
        for (std::size_t i = 0; i < workflow_.tasks.size(); ++i) {
            const auto& task = workflow_.tasks[i];
            if (task.id.empty()) fail("task id may not be empty");
            if (!index_.emplace(task.id, i).second) fail("duplicate task id: " + task.id);
            if (task.mean_ms <= 0.0) fail("task duration must be positive: " + task.id);
            if (task.jitter < 0.0 || task.jitter >= 1.0) fail("jitter must be in [0,1): " + task.id);
            if (task.failure_probability < 0.0 || task.failure_probability > 1.0) fail("failure probability must be in [0,1]");
            if (task.max_attempts < 1) fail("max attempts must be positive");
        }
        children_.resize(workflow_.tasks.size());
        indegree_.assign(workflow_.tasks.size(), 0);
        for (std::size_t i = 0; i < workflow_.tasks.size(); ++i) {
            for (const auto& dependency : workflow_.tasks[i].depends_on) {
                const auto it = index_.find(dependency);
                if (it == index_.end()) fail("unknown dependency " + dependency + " for " + workflow_.tasks[i].id);
                if (it->second == i) fail("task depends on itself: " + workflow_.tasks[i].id);
                children_[it->second].push_back(i);
                ++indegree_[i];
            }
        }
        validate_acyclic();
        calculate_critical_tail();
    }

    const Workflow& workflow() const { return workflow_; }
    std::size_t size() const { return workflow_.tasks.size(); }
    const Task& task(std::size_t i) const { return workflow_.tasks.at(i); }
    std::size_t index_of(const std::string& id) const { return index_.at(id); }
    const std::vector<std::size_t>& children(std::size_t i) const { return children_.at(i); }
    int indegree(std::size_t i) const { return indegree_.at(i); }
    double critical_tail(std::size_t i) const { return critical_tail_.at(i); }

    std::vector<std::size_t> topological_order() const {
        std::vector<int> indegree = indegree_;
        std::priority_queue<std::size_t, std::vector<std::size_t>, std::greater<>> queue;
        for (std::size_t i = 0; i < indegree.size(); ++i) if (indegree[i] == 0) queue.push(i);
        std::vector<std::size_t> result;
        while (!queue.empty()) {
            const auto current = queue.top();
            queue.pop();
            result.push_back(current);
            for (const auto child : children_[current]) {
                if (--indegree[child] == 0) queue.push(child);
            }
        }
        return result;
    }

    double ideal_critical_path_ms() const {
        double result = 0.0;
        for (std::size_t i = 0; i < size(); ++i) result = std::max(result, critical_tail_[i]);
        return result;
    }

private:
    Workflow workflow_;
    std::unordered_map<std::string, std::size_t> index_;
    std::vector<std::vector<std::size_t>> children_;
    std::vector<int> indegree_;
    std::vector<double> critical_tail_;

    void validate_acyclic() const {
        if (topological_order().size() != workflow_.tasks.size()) fail("workflow contains a dependency cycle");
    }

    void calculate_critical_tail() {
        critical_tail_.assign(size(), 0.0);
        auto order = topological_order();
        std::reverse(order.begin(), order.end());
        for (const auto index : order) {
            double child_tail = 0.0;
            for (const auto child : children_[index]) child_tail = std::max(child_tail, critical_tail_[child]);
            critical_tail_[index] = task(index).mean_ms + child_tail;
        }
    }
};

template <typename Engine>
AttemptResult sample_attempt(const Task& task, Engine& rng) {
    const double sigma = std::max(0.0001, task.jitter);
    // Lognormal samples avoid physically impossible negative runtimes while preserving
    // a long tail that looks more like builds/tests than a symmetric normal distribution.
    const double mu = std::log(task.mean_ms) - 0.5 * sigma * sigma;
    std::lognormal_distribution<double> duration(mu, sigma);
    std::bernoulli_distribution failure(task.failure_probability);
    return AttemptResult{!failure(rng), std::max(1.0, duration(rng))};
}

class Simulator {
public:
    Simulator(const Graph& graph, int workers) : graph_(graph), workers_(workers) {}

    template <typename Engine>
    RunResult run(Engine& rng, bool trace) const {
        const auto n = graph_.size();
        std::vector<Runtime> runtime(n);
        std::vector<int> remaining_dependencies(n);
        for (std::size_t i = 0; i < n; ++i) remaining_dependencies[i] = graph_.indegree(i);

        auto ready_compare = [&](std::size_t lhs, std::size_t rhs) {
            const auto lhs_tail = graph_.critical_tail(lhs);
            const auto rhs_tail = graph_.critical_tail(rhs);
            if (lhs_tail != rhs_tail) return lhs_tail < rhs_tail;
            return graph_.task(lhs).id > graph_.task(rhs).id;
        };
        std::priority_queue<std::size_t, std::vector<std::size_t>, decltype(ready_compare)> ready(ready_compare);
        for (std::size_t i = 0; i < n; ++i) {
            if (remaining_dependencies[i] == 0) {
                runtime[i].state = State::ready;
                ready.push(i);
            }
        }

        auto running_compare = [](const Running& lhs, const Running& rhs) {
            if (lhs.finish_at != rhs.finish_at) return lhs.finish_at > rhs.finish_at;
            return lhs.task_id > rhs.task_id;
        };
        std::priority_queue<Running, std::vector<Running>, decltype(running_compare)> running(running_compare);
        std::set<int> free_workers;
        for (int worker = 0; worker < workers_; ++worker) free_workers.insert(worker);

        double now = 0.0;
        double worker_busy_ms = 0.0;
        int attempts = 0;
        int failures = 0;
        std::size_t succeeded = 0;
        bool terminal_failure = false;

        auto log = [&](const std::string& text) {
            if (trace) std::cerr << std::fixed << std::setprecision(1) << "[" << now << "ms] " << text << '\n';
        };

        while (succeeded < n && !terminal_failure) {
            while (!ready.empty() && !free_workers.empty()) {
                const auto index = ready.top();
                ready.pop();
                auto& rt = runtime[index];
                if (rt.state != State::ready) continue;

                const int worker = *free_workers.begin();
                free_workers.erase(free_workers.begin());
                const auto result = sample_attempt(graph_.task(index), rng);
                ++rt.attempts;
                ++attempts;
                rt.state = State::running;
                rt.started_at = now;
                rt.worker = worker;
                worker_busy_ms += result.duration_ms;
                running.push(Running{graph_.task(index).id, worker, now + result.duration_ms, result.success});
                log("worker " + std::to_string(worker) + " started " + graph_.task(index).id + " attempt " + std::to_string(rt.attempts));
            }

            if (running.empty()) {
                if (succeeded != n) terminal_failure = true;
                break;
            }

            const double next_time = running.top().finish_at;
            now = next_time;
            std::vector<Running> completed;
            while (!running.empty() && std::abs(running.top().finish_at - next_time) < 1e-9) {
                completed.push_back(running.top());
                running.pop();
            }

            for (const auto& completion : completed) {
                const auto index = graph_.index_of(completion.task_id);
                auto& rt = runtime[index];
                free_workers.insert(completion.worker);
                rt.finished_at = now;
                rt.worker = -1;

                if (completion.success) {
                    rt.state = State::succeeded;
                    ++succeeded;
                    log("worker " + std::to_string(completion.worker) + " completed " + completion.task_id);
                    for (const auto child : graph_.children(index)) {
                        if (--remaining_dependencies[child] == 0 && runtime[child].state == State::pending) {
                            runtime[child].state = State::ready;
                            ready.push(child);
                            log(graph_.task(child).id + " became ready");
                        }
                    }
                } else {
                    ++failures;
                    log("worker " + std::to_string(completion.worker) + " failed " + completion.task_id);
                    if (rt.attempts < graph_.task(index).max_attempts) {
                        rt.state = State::ready;
                        ready.push(index);
                    } else {
                        rt.state = State::failed;
                        terminal_failure = true;
                        log(completion.task_id + " exhausted retry budget");
                    }
                }
            }
        }

        return RunResult{
            !terminal_failure && succeeded == n,
            now,
            attempts,
            failures,
            worker_busy_ms,
        };
    }

private:
    const Graph& graph_;
    int workers_;
};

double percentile(std::vector<double> values, double p) {
    if (values.empty()) return 0.0;
    std::sort(values.begin(), values.end());
    const double pos = p * static_cast<double>(values.size() - 1);
    const auto lower = static_cast<std::size_t>(std::floor(pos));
    const auto upper = static_cast<std::size_t>(std::ceil(pos));
    if (lower == upper) return values[lower];
    const double fraction = pos - static_cast<double>(lower);
    return values[lower] * (1.0 - fraction) + values[upper] * fraction;
}

Summary summarize(const std::vector<RunResult>& runs, int workers) {
    if (runs.empty()) return {};
    std::vector<double> successful_times;
    successful_times.reserve(runs.size());
    double attempts = 0.0;
    double failures = 0.0;
    double utilization = 0.0;
    int successes = 0;

    for (const auto& run : runs) {
        if (run.success) {
            ++successes;
            successful_times.push_back(run.makespan_ms);
        }
        attempts += static_cast<double>(run.attempts);
        failures += static_cast<double>(run.failures);
        if (run.makespan_ms > 0.0) {
            utilization += run.worker_busy_ms / (run.makespan_ms * static_cast<double>(workers));
        }
    }

    const double mean = successful_times.empty() ? 0.0
        : std::accumulate(successful_times.begin(), successful_times.end(), 0.0) / static_cast<double>(successful_times.size());
    double variance = 0.0;
    for (const auto value : successful_times) {
        const auto delta = value - mean;
        variance += delta * delta;
    }
    if (!successful_times.empty()) variance /= static_cast<double>(successful_times.size());

    return Summary{
        static_cast<int>(runs.size()),
        successes,
        mean,
        std::sqrt(variance),
        percentile(successful_times, 0.50),
        percentile(successful_times, 0.90),
        percentile(successful_times, 0.95),
        percentile(successful_times, 0.99),
        successful_times.empty() ? 0.0 : *std::min_element(successful_times.begin(), successful_times.end()),
        successful_times.empty() ? 0.0 : *std::max_element(successful_times.begin(), successful_times.end()),
        attempts / static_cast<double>(runs.size()),
        failures / static_cast<double>(runs.size()),
        utilization / static_cast<double>(runs.size()),
    };
}

std::string json_escape(std::string_view value) {
    std::ostringstream out;
    for (const char c : value) {
        switch (c) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<int>(c);
                } else out << c;
        }
    }
    return out.str();
}

void print_dot(const Graph& graph) {
    std::cout << "digraph helixgrid {\n"
              << "  rankdir=LR;\n"
              << "  graph [fontname=\"Inter\"];\n"
              << "  node [shape=box style=rounded fontname=\"Inter\"];\n"
              << "  edge [fontname=\"Inter\"];\n";
    for (const auto& task : graph.workflow().tasks) {
        std::cout << "  \"" << json_escape(task.id) << "\" [label=\""
                  << json_escape(task.id) << "\\n" << std::fixed << std::setprecision(0)
                  << task.mean_ms << "ms\"];\n";
    }
    for (const auto& task : graph.workflow().tasks) {
        for (const auto& dependency : task.depends_on) {
            std::cout << "  \"" << json_escape(dependency) << "\" -> \"" << json_escape(task.id) << "\";\n";
        }
    }
    std::cout << "}\n";
}

void print_json(const Graph& graph, const Options& options, const Summary& summary) {
    std::cout << std::fixed << std::setprecision(3)
              << "{\n"
              << "  \"workflow\": \"" << json_escape(graph.workflow().name) << "\",\n"
              << "  \"tasks\": " << graph.size() << ",\n"
              << "  \"workers\": " << options.workers << ",\n"
              << "  \"runs\": " << summary.runs << ",\n"
              << "  \"seed\": " << options.seed << ",\n"
              << "  \"success_rate\": " << (summary.runs ? static_cast<double>(summary.successes) / summary.runs : 0.0) << ",\n"
              << "  \"ideal_critical_path_ms\": " << graph.ideal_critical_path_ms() << ",\n"
              << "  \"mean_ms\": " << summary.mean_ms << ",\n"
              << "  \"stddev_ms\": " << summary.stddev_ms << ",\n"
              << "  \"p50_ms\": " << summary.p50_ms << ",\n"
              << "  \"p90_ms\": " << summary.p90_ms << ",\n"
              << "  \"p95_ms\": " << summary.p95_ms << ",\n"
              << "  \"p99_ms\": " << summary.p99_ms << ",\n"
              << "  \"min_ms\": " << summary.min_ms << ",\n"
              << "  \"max_ms\": " << summary.max_ms << ",\n"
              << "  \"mean_attempts\": " << summary.mean_attempts << ",\n"
              << "  \"mean_failures\": " << summary.mean_failures << ",\n"
              << "  \"mean_worker_utilization\": " << summary.mean_utilization << "\n"
              << "}\n";
}

void print_human(const Graph& graph, const Options& options, const Summary& summary) {
    const double success_rate = summary.runs ? (100.0 * static_cast<double>(summary.successes) / summary.runs) : 0.0;
    std::cout << "HelixGrid scheduler simulation\n"
              << "========================================\n"
              << "workflow          " << graph.workflow().name << '\n'
              << "tasks             " << graph.size() << '\n'
              << "workers           " << options.workers << '\n'
              << "runs              " << summary.runs << '\n'
              << "seed              " << options.seed << '\n'
              << std::fixed << std::setprecision(2)
              << "success rate      " << success_rate << "%\n"
              << "critical path     " << graph.ideal_critical_path_ms() << " ms (mean durations)\n"
              << "mean makespan     " << summary.mean_ms << " ms\n"
              << "std deviation     " << summary.stddev_ms << " ms\n"
              << "p50 / p90         " << summary.p50_ms << " / " << summary.p90_ms << " ms\n"
              << "p95 / p99         " << summary.p95_ms << " / " << summary.p99_ms << " ms\n"
              << "min / max         " << summary.min_ms << " / " << summary.max_ms << " ms\n"
              << "mean attempts     " << summary.mean_attempts << '\n'
              << "mean failures     " << summary.mean_failures << '\n'
              << "worker utilization " << (summary.mean_utilization * 100.0) << "%\n";
}

} // namespace helix

int main(int argc, char** argv) {
    try {
        const auto options = helix::parse_args(argc, argv);
        const auto workflow = helix::demo_workflow();
        const helix::Graph graph(workflow);
        if (options.dot) {
            helix::print_dot(graph);
            return 0;
        }

        helix::Simulator simulator(graph, options.workers);
        std::mt19937_64 rng(options.seed);
        std::vector<helix::RunResult> results;
        results.reserve(static_cast<std::size_t>(options.runs));
        for (int run = 0; run < options.runs; ++run) {
            results.push_back(simulator.run(rng, options.verbose && run == 0));
        }
        const auto summary = helix::summarize(results, options.workers);
        if (options.json) helix::print_json(graph, options, summary);
        else helix::print_human(graph, options, summary);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "helix-sim: " << error.what() << '\n';
        return 1;
    }
}
