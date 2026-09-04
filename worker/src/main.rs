use anyhow::{Context, Result, anyhow};
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::{collections::HashMap, env, process::Stdio, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
    sync::{RwLock, Semaphore, mpsc},
    task::JoinSet,
    time::{Instant, MissedTickBehavior, interval, sleep, timeout},
};
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};

#[derive(Debug, Clone)]
struct Config {
    coordinator: String,
    worker_name: String,
    version: String,
    concurrency: usize,
    poll_interval: Duration,
    heartbeat_interval: Duration,
    lease_renew_interval: Duration,
    request_timeout: Duration,
    labels: HashMap<String, String>,
}

impl Config {
    fn from_env() -> Result<Self> {
        let coordinator = env::var("HELIX_COORDINATOR")
            .unwrap_or_else(|_| "http://127.0.0.1:8080".into())
            .trim_end_matches('/')
            .to_string();
        let hostname = env::var("HOSTNAME").unwrap_or_else(|_| "local".into());
        let worker_name = env::var("HELIX_WORKER_NAME")
            .unwrap_or_else(|_| format!("worker-{hostname}"));
        let concurrency = parse_env("HELIX_CONCURRENCY", 4usize)?;
        if concurrency == 0 || concurrency > 256 {
            return Err(anyhow!("HELIX_CONCURRENCY must be between 1 and 256"));
        }
        let labels = parse_labels(&env::var("HELIX_LABELS").unwrap_or_default())?;
        let poll_ms = positive_setting("HELIX_POLL_MS", parse_env("HELIX_POLL_MS", 400u64)?)?;
        let heartbeat_seconds = positive_setting(
            "HELIX_HEARTBEAT_SECONDS",
            parse_env("HELIX_HEARTBEAT_SECONDS", 10u64)?,
        )?;
        let lease_renew_seconds = positive_setting(
            "HELIX_LEASE_RENEW_SECONDS",
            parse_env("HELIX_LEASE_RENEW_SECONDS", 6u64)?,
        )?;
        let request_timeout_seconds = positive_setting(
            "HELIX_HTTP_TIMEOUT_SECONDS",
            parse_env("HELIX_HTTP_TIMEOUT_SECONDS", 15u64)?,
        )?;
        Ok(Self {
            coordinator,
            worker_name,
            version: env!("CARGO_PKG_VERSION").into(),
            concurrency,
            poll_interval: Duration::from_millis(poll_ms),
            heartbeat_interval: Duration::from_secs(heartbeat_seconds),
            lease_renew_interval: Duration::from_secs(lease_renew_seconds),
            request_timeout: Duration::from_secs(request_timeout_seconds),
            labels,
        })
    }
}

fn parse_env<T>(key: &str, default: T) -> Result<T>
where
    T: std::str::FromStr,
    T::Err: std::fmt::Display,
{
    match env::var(key) {
        Ok(raw) => raw
            .parse::<T>()
            .map_err(|e| anyhow!("invalid {key}: {e}")),
        Err(_) => Ok(default),
    }
}

fn positive_setting(key: &str, value: u64) -> Result<u64> {
    if value == 0 {
        return Err(anyhow!("{key} must be greater than zero"));
    }
    Ok(value)
}

fn parse_labels(raw: &str) -> Result<HashMap<String, String>> {
    let mut out = HashMap::new();
    for item in raw.split(',').map(str::trim).filter(|v| !v.is_empty()) {
        let (key, value) = item
            .split_once('=')
            .ok_or_else(|| anyhow!("invalid HELIX_LABELS item {item:?}; expected key=value"))?;
        let key = key.trim();
        let value = value.trim();
        if key.is_empty() || value.is_empty() {
            return Err(anyhow!("label keys and values may not be empty"));
        }
        out.insert(key.into(), value.into());
    }
    Ok(out)
}

#[derive(Clone)]
struct Api {
    client: Client,
    base: String,
}

#[derive(Debug, Deserialize)]
struct Envelope<T> {
    data: T,
}

#[derive(Debug, Serialize)]
struct RegisterWorkerRequest<'a> {
    name: &'a str,
    version: &'a str,
    labels: &'a HashMap<String, String>,
    capacity: usize,
}

#[derive(Debug, Clone, Deserialize)]
struct Worker {
    id: String,
    name: String,
    capacity: usize,
}

#[derive(Debug, Serialize)]
struct LeaseRequest<'a> {
    worker_id: &'a str,
}

#[derive(Debug, Clone, Deserialize)]
struct RetryPolicy {
    #[allow(dead_code)]
    max_attempts: Option<u32>,
    #[allow(dead_code)]
    base_delay_ms: Option<u64>,
    #[allow(dead_code)]
    max_delay_ms: Option<u64>,
}

#[derive(Debug, Clone, Deserialize)]
struct TaskSpec {
    id: String,
    #[serde(default)]
    depends_on: Vec<String>,
    command: Vec<String>,
    #[serde(default)]
    env: HashMap<String, String>,
    #[serde(default)]
    timeout_seconds: u64,
    #[serde(default)]
    retry: Option<RetryPolicy>,
    #[serde(default)]
    labels: HashMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
struct Lease {
    token: String,
    workflow_id: String,
    task_id: String,
    worker_id: String,
    attempt: u32,
    expires_at: String,
    spec: TaskSpec,
}

#[derive(Debug, Serialize)]
struct LogRequest<'a> {
    stream: &'a str,
    text: &'a str,
}

#[derive(Debug, Serialize)]
struct CompleteRequest<'a> {
    exit_code: i32,
    #[serde(skip_serializing_if = "str::is_empty")]
    error: &'a str,
}

#[derive(Debug)]
struct CoordinatorHttpError {
    status: StatusCode,
    body: String,
}

impl std::fmt::Display for CoordinatorHttpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "coordinator returned {}: {}", self.status, self.body)
    }
}

impl std::error::Error for CoordinatorHttpError {}

fn coordinator_status(error: &anyhow::Error) -> Option<StatusCode> {
    error
        .downcast_ref::<CoordinatorHttpError>()
        .map(|http_error| http_error.status)
}


impl Api {
    fn new(config: &Config) -> Result<Self> {
        let client = Client::builder()
            .timeout(config.request_timeout)
            .tcp_keepalive(Duration::from_secs(30))
            .user_agent(format!("helix-worker/{}", config.version))
            .build()
            .context("failed to build HTTP client")?;
        Ok(Self { client, base: config.coordinator.clone() })
    }

    async fn register(&self, config: &Config) -> Result<Worker> {
        let response = self.client
            .post(format!("{}/v1/workers/register", self.base))
            .json(&RegisterWorkerRequest {
                name: &config.worker_name,
                version: &config.version,
                labels: &config.labels,
                capacity: config.concurrency,
            })
            .send().await.context("register request failed")?;
        decode_response::<Envelope<Worker>>(response).await.map(|v| v.data)
    }

    async fn heartbeat(&self, worker_id: &str) -> Result<()> {
        let response = self.client
            .post(format!("{}/v1/workers/{worker_id}/heartbeat", self.base))
            .send().await.context("heartbeat request failed")?;
        if response.status().is_success() { Ok(()) } else { Err(response_error(response).await) }
    }

    async fn lease(&self, worker_id: &str) -> Result<Option<Lease>> {
        let response = self.client
            .post(format!("{}/v1/leases", self.base))
            .json(&LeaseRequest { worker_id })
            .send().await.context("lease request failed")?;
        if response.status() == StatusCode::NO_CONTENT { return Ok(None); }
        decode_response::<Envelope<Lease>>(response).await.map(|v| Some(v.data))
    }

    async fn start(&self, token: &str) -> Result<()> {
        self.empty_post(format!("{}/v1/leases/{token}/start", self.base)).await
    }

    async fn renew(&self, token: &str) -> Result<Lease> {
        let response = self.client
            .post(format!("{}/v1/leases/{token}/renew", self.base))
            .send().await.context("renew request failed")?;
        decode_response::<Envelope<Lease>>(response).await.map(|v| v.data)
    }

    async fn log(&self, token: &str, stream: &str, text: &str) -> Result<()> {
        let response = self.client
            .post(format!("{}/v1/leases/{token}/logs", self.base))
            .json(&LogRequest { stream, text })
            .send().await.context("log request failed")?;
        if response.status().is_success() { Ok(()) } else { Err(response_error(response).await) }
    }

    async fn complete(&self, token: &str, exit_code: i32, error: &str) -> Result<()> {
        let response = self.client
            .post(format!("{}/v1/leases/{token}/complete", self.base))
            .json(&CompleteRequest { exit_code, error })
            .send().await.context("complete request failed")?;
        if response.status().is_success() { Ok(()) } else { Err(response_error(response).await) }
    }

    async fn empty_post(&self, url: String) -> Result<()> {
        let response = self.client.post(url).send().await?;
        if response.status().is_success() { Ok(()) } else { Err(response_error(response).await) }
    }
}

async fn decode_response<T: DeserializeOwned>(response: reqwest::Response) -> Result<T> {
    let status = response.status();
    if !status.is_success() {
        return Err(response_error(response).await);
    }
    response.json::<T>().await.context("invalid coordinator response")
}

async fn response_error(response: reqwest::Response) -> anyhow::Error {
    let status = response.status();
    let body = response
        .text()
        .await
        .unwrap_or_else(|_| "<unreadable body>".into());
    anyhow::Error::new(CoordinatorHttpError { status, body })
}

#[derive(Debug)]
struct LogChunk {
    stream: &'static str,
    text: String,
}

#[derive(Debug)]
struct ExecutionResult {
    exit_code: i32,
    error: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "helix_worker=info".into()))
        .json()
        .init();

    let config = Arc::new(Config::from_env()?);
    let api = Arc::new(Api::new(&config)?);
    let shutdown = CancellationToken::new();

    let signal_shutdown = shutdown.clone();
    tokio::spawn(async move {
        wait_for_shutdown_signal().await;
        signal_shutdown.cancel();
    });

    info!(coordinator=%config.coordinator, concurrency=config.concurrency, "starting worker");
    let worker = register_with_retry(&api, &config, &shutdown).await?;
    info!(worker_id=%worker.id, worker_name=%worker.name, capacity=worker.capacity, "worker registered");
    let worker_session = Arc::new(RwLock::new(worker));

    let heartbeat_shutdown = shutdown.clone();
    let heartbeat_api = api.clone();
    let heartbeat_config = config.clone();
    let heartbeat_session = worker_session.clone();
    let heartbeat_every = config.heartbeat_interval;
    let heartbeat_task = tokio::spawn(async move {
        heartbeat_loop(
            heartbeat_api,
            heartbeat_config,
            heartbeat_session,
            heartbeat_every,
            heartbeat_shutdown,
        )
        .await;
    });

    let semaphore = Arc::new(Semaphore::new(config.concurrency));
    let mut tasks = JoinSet::new();
    let mut backoff = Duration::from_millis(100);

    loop {
        if shutdown.is_cancelled() { break; }
        while let Some(result) = tasks.try_join_next() {
            if let Err(join_error) = result {
                error!(error=%join_error, "task executor panicked");
            }
        }

        let permit = tokio::select! {
            _ = shutdown.cancelled() => break,
            permit = semaphore.clone().acquire_owned() => permit.context("semaphore closed")?,
        };

        let worker_id = { worker_session.read().await.id.clone() };
        match api.lease(&worker_id).await {
            Ok(Some(lease)) => {
                backoff = Duration::from_millis(100);
                let api = api.clone();
                let shutdown = shutdown.clone();
                let renew_every = config.lease_renew_interval;
                tasks.spawn(async move {
                    let _permit = permit;
                    if let Err(e) = run_lease(api, lease, renew_every, shutdown).await {
                        error!(error=%e, "lease execution failed");
                    }
                });
            }
            Ok(None) => {
                drop(permit);
                tokio::select! {
                    _ = shutdown.cancelled() => break,
                    _ = sleep(config.poll_interval) => {}
                }
            }
            Err(err) => {
                drop(permit);
                warn!(error=%err, retry_ms=backoff.as_millis(), "lease polling failed");
                tokio::select! {
                    _ = shutdown.cancelled() => break,
                    _ = sleep(backoff) => {}
                }
                backoff = (backoff * 2).min(Duration::from_secs(5));
            }
        }
    }

    info!("worker stopping; waiting for active tasks");
    let drain_deadline = Instant::now() + Duration::from_secs(20);
    while !tasks.is_empty() && Instant::now() < drain_deadline {
        if let Some(Err(err)) = tasks.join_next().await {
            warn!(error=%err, "executor join failed during shutdown");
        }
    }
    tasks.abort_all();
    let _ = heartbeat_task.await;
    info!("worker stopped");
    Ok(())
}

async fn register_with_retry(api: &Api, config: &Config, shutdown: &CancellationToken) -> Result<Worker> {
    let mut delay = Duration::from_millis(250);
    loop {
        match api.register(config).await {
            Ok(worker) => return Ok(worker),
            Err(err) => {
                warn!(error=%err, retry_ms=delay.as_millis(), "worker registration failed");
                tokio::select! {
                    _ = shutdown.cancelled() => return Err(anyhow!("shutdown before registration")),
                    _ = sleep(delay) => {}
                }
                delay = (delay * 2).min(Duration::from_secs(10));
            }
        }
    }
}

async fn heartbeat_loop(
    api: Arc<Api>,
    config: Arc<Config>,
    session: Arc<RwLock<Worker>>,
    every: Duration,
    shutdown: CancellationToken,
) {
    let mut ticker = interval(every);
    ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
    ticker.tick().await;
    loop {
        tokio::select! {
            _ = shutdown.cancelled() => return,
            _ = ticker.tick() => {
                let worker_id = { session.read().await.id.clone() };
                match api.heartbeat(&worker_id).await {
                    Ok(()) => debug!(worker_id=%worker_id, "heartbeat sent"),
                    Err(err) => {
                        warn!(error=%err, worker_id=%worker_id, "heartbeat failed");
                        if coordinator_status(&err) == Some(StatusCode::NOT_FOUND) {
                            match api.register(&config).await {
                                Ok(replacement) => {
                                    let replacement_id = replacement.id.clone();
                                    *session.write().await = replacement;
                                    info!(
                                        old_worker_id=%worker_id,
                                        worker_id=%replacement_id,
                                        "coordinator forgot worker; registered a fresh worker session"
                                    );
                                }
                                Err(register_err) => {
                                    warn!(error=%register_err, "worker re-registration failed");
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

async fn run_lease(api: Arc<Api>, lease: Lease, renew_every: Duration, shutdown: CancellationToken) -> Result<()> {
    let token = lease.token.clone();
    info!(workflow_id=%lease.workflow_id, task_id=%lease.task_id, worker_id=%lease.worker_id, attempt=lease.attempt, expires_at=%lease.expires_at, "lease acquired");
    api.start(&token).await.context("failed to acknowledge lease start")?;

    let lease_cancel = CancellationToken::new();
    let renew_cancel = lease_cancel.clone();
    let renew_api = api.clone();
    let renew_token = token.clone();
    let renew_handle = tokio::spawn(async move {
        let mut ticker = interval(renew_every);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
        ticker.tick().await;
        loop {
            tokio::select! {
                _ = renew_cancel.cancelled() => return,
                _ = ticker.tick() => match renew_api.renew(&renew_token).await {
                    Ok(_) => debug!(lease=%renew_token, "lease renewed"),
                    Err(err) => {
                        warn!(lease=%renew_token, error=%err, "lease renewal failed");
                        return;
                    }
                }
            }
        }
    });

    let result = execute_task(api.clone(), &lease, shutdown).await;
    lease_cancel.cancel();
    let _ = renew_handle.await;

    match result {
        Ok(result) => {
            if let Err(err) = api.complete(&token, result.exit_code, &result.error).await {
                return Err(err).context("completion report failed");
            }
            info!(workflow_id=%lease.workflow_id, task_id=%lease.task_id, exit_code=result.exit_code, "task completed");
        }
        Err(err) => {
            let message = format!("worker execution error: {err:#}");
            if let Err(report_err) = api.complete(&token, 125, &message).await {
                warn!(error=%report_err, "failed to report worker-side execution error");
            }
            return Err(err);
        }
    }
    Ok(())
}

async fn execute_task(api: Arc<Api>, lease: &Lease, shutdown: CancellationToken) -> Result<ExecutionResult> {
    let spec = &lease.spec;
    if spec.command.is_empty() { return Err(anyhow!("empty command")); }

    debug!(task_id=%spec.id, deps=?spec.depends_on, labels=?spec.labels, retry=?spec.retry, "executing task specification");
    let mut command = Command::new(&spec.command[0]);
    command.args(&spec.command[1..]);
    command.envs(&spec.env);
    command.stdin(Stdio::null());
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());
    command.kill_on_drop(true);

    let mut child = command.spawn().with_context(|| format!("failed to spawn {:?}", spec.command))?;
    let stdout = child.stdout.take().context("missing stdout pipe")?;
    let stderr = child.stderr.take().context("missing stderr pipe")?;
    let (log_tx, mut log_rx) = mpsc::channel::<LogChunk>(128);

    let out_tx = log_tx.clone();
    let stdout_task = tokio::spawn(async move { read_stream(stdout, "stdout", out_tx).await });
    let err_tx = log_tx.clone();
    let stderr_task = tokio::spawn(async move { read_stream(stderr, "stderr", err_tx).await });
    drop(log_tx);

    let log_api = api.clone();
    let log_token = lease.token.clone();
    let log_task = tokio::spawn(async move {
        while let Some(chunk) = log_rx.recv().await {
            if let Err(err) = log_api.log(&log_token, chunk.stream, &chunk.text).await {
                warn!(error=%err, stream=chunk.stream, "log forwarding failed");
            }
        }
    });

    let wait_future = async {
        tokio::select! {
            _ = shutdown.cancelled() => {
                let _ = child.kill().await;
                Ok::<ExecutionResult, anyhow::Error>(ExecutionResult { exit_code: 130, error: "worker shutdown".into() })
            }
            status = child.wait() => {
                let status = status.context("failed waiting for child")?;
                let exit_code = status.code().unwrap_or(128);
                let error = if status.success() { String::new() } else { format!("process exited with {status}") };
                Ok(ExecutionResult { exit_code, error })
            }
        }
    };

    let result = if spec.timeout_seconds > 0 {
        match timeout(Duration::from_secs(spec.timeout_seconds), wait_future).await {
            Ok(result) => result?,
            Err(_) => {
                child
                    .kill()
                    .await
                    .context("failed to kill process after task timeout")?;
                ExecutionResult {
                    exit_code: 124,
                    error: format!("task timed out after {} seconds", spec.timeout_seconds),
                }
            }
        }
    } else {
        wait_future.await?
    };

    let _ = stdout_task.await;
    let _ = stderr_task.await;
    let _ = log_task.await;
    Ok(result)
}

async fn read_stream<R>(reader: R, stream: &'static str, tx: mpsc::Sender<LogChunk>)
where
    R: tokio::io::AsyncRead + Unpin,
{
    let mut lines = BufReader::new(reader).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                let mut text = line;
                text.push('\n');
                if tx.send(LogChunk { stream, text }).await.is_err() { return; }
            }
            Ok(None) => return,
            Err(err) => {
                let _ = tx.send(LogChunk { stream: "stderr", text: format!("[helix stream error: {err}]\n") }).await;
                return;
            }
        }
    }
}

#[cfg(unix)]
async fn wait_for_shutdown_signal() {
    use tokio::signal::unix::{SignalKind, signal};
    let mut term = signal(SignalKind::terminate()).expect("SIGTERM handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
}

#[cfg(not(unix))]
async fn wait_for_shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_labels() {
        let labels = parse_labels("os=linux, arch=x86_64").unwrap();
        assert_eq!(labels.get("os").unwrap(), "linux");
        assert_eq!(labels.get("arch").unwrap(), "x86_64");
    }

    #[test]
    fn rejects_broken_labels() {
        assert!(parse_labels("linux").is_err());
        assert!(parse_labels("=value").is_err());
        assert!(parse_labels("key=").is_err());
    }

    #[test]
    fn rejects_zero_timing_settings() {
        assert!(positive_setting("HELIX_POLL_MS", 0).is_err());
        assert!(positive_setting("HELIX_HEARTBEAT_SECONDS", 0).is_err());
        assert_eq!(positive_setting("HELIX_POLL_MS", 1).unwrap(), 1);
    }

    #[test]
    fn preserves_http_status_for_recovery_decisions() {
        let error = anyhow::Error::new(CoordinatorHttpError {
            status: StatusCode::NOT_FOUND,
            body: "missing".into(),
        });
        assert_eq!(coordinator_status(&error), Some(StatusCode::NOT_FOUND));
    }
}
