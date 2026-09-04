from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from tkinter import Tk, StringVar, BooleanVar, IntVar, Text, END, filedialog, messagebox
from tkinter import ttk

APP_NAME = "HelixGrid"
API_BASE = "http://127.0.0.1:8080"
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / "HelixGrid"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class DashboardConfig:
    workspace: str
    results: str
    workers: int = 3
    auto_start: bool = True

    @classmethod
    def default(cls) -> "DashboardConfig":
        root = repo_root()
        return cls(
            workspace=str(root / "workspace"),
            results=str(root / "helix-results"),
            workers=3,
            auto_start=True,
        )

    @classmethod
    def load(cls) -> "DashboardConfig":
        path = app_data_dir() / "dashboard.json"
        if not path.exists():
            return cls.default()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            default = cls.default()
            return cls(
                workspace=str(raw.get("workspace") or default.workspace),
                results=str(raw.get("results") or default.results),
                workers=max(1, min(16, int(raw.get("workers", 3)))),
                auto_start=bool(raw.get("auto_start", True)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return cls.default()

    def save(self) -> None:
        path = app_data_dir() / "dashboard.json"
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")


def hidden_subprocess_kwargs() -> dict:
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def docker_env(config: DashboardConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["HELIX_WORKSPACE"] = str(Path(config.workspace).resolve())
    env["HELIX_RESULTS"] = str(Path(config.results).resolve())
    return env


def run_command(args: list[str], *, config: DashboardConfig, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=repo_root(),
        env=docker_env(config),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
        **hidden_subprocess_kwargs(),
    )


def docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=8,
            **hidden_subprocess_kwargs(),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def api_request(method: str, path: str, body: object | None = None, timeout: float = 5.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(API_BASE + path, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return {}
    return json.loads(raw)


def coordinator_online() -> bool:
    try:
        payload = api_request("GET", "/healthz", timeout=1.5)
        return payload.get("status") == "ok"
    except Exception:
        return False


def launch_docker_desktop() -> bool:
    if docker_available():
        return True
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Docker" / "Docker Desktop.exe",
    ]
    for executable in candidates:
        if executable.exists():
            subprocess.Popen(
                [str(executable)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **hidden_subprocess_kwargs(),
            )
            return True
    return False


class HelixDashboard(Tk):
    BG = "#0b1020"
    PANEL = "#121a2f"
    PANEL_2 = "#17213a"
    TEXT = "#f4f7fb"
    MUTED = "#91a0ba"
    ACCENT = "#63a7ff"
    SUCCESS = "#54d38a"
    DANGER = "#ff6b7d"
    WARNING = "#f7c85c"

    def __init__(self) -> None:
        super().__init__()
        self.title("HelixGrid")
        self.geometry("1180x760")
        self.minsize(980, 650)
        self.configure(bg=self.BG)
        self.config_state = DashboardConfig.load()
        Path(self.config_state.workspace).mkdir(parents=True, exist_ok=True)
        Path(self.config_state.results).mkdir(parents=True, exist_ok=True)

        self.workspace_var = StringVar(value=self.config_state.workspace)
        self.results_var = StringVar(value=self.config_state.results)
        self.workers_var = IntVar(value=self.config_state.workers)
        self.auto_start_var = BooleanVar(value=self.config_state.auto_start)
        self.status_var = StringVar(value="Kontrollerer systemet…")
        self.docker_var = StringVar(value="Kontrollerer…")
        self.coordinator_var = StringVar(value="Kontrollerer…")
        self.worker_var = StringVar(value="0 workers")
        self.workflow_var = StringVar(value="0 workflows")
        self.activity_var = StringVar(value="Klar")
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False
        self._last_workflows: list[dict] = []

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)
        self.after(250, self.refresh_status)
        self.after(2500, self._periodic_refresh)
        if self.config_state.auto_start:
            self.after(900, self.ensure_started)

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=self.BG, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("Card.TFrame", background=self.PANEL_2)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT)
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI Semibold", 26))
        style.configure("Subtitle.TLabel", background=self.BG, foreground=self.MUTED, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT)
        style.configure("CardTitle.TLabel", background=self.PANEL_2, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("CardValue.TLabel", background=self.PANEL_2, foreground=self.TEXT, font=("Segoe UI Semibold", 15))
        style.configure("TButton", padding=(14, 9), font=("Segoe UI Semibold", 10))
        style.configure("Accent.TButton", background=self.ACCENT, foreground="#07111f")
        style.map("Accent.TButton", background=[("active", "#84baff")])
        style.configure("Danger.TButton", background=self.DANGER, foreground="#19080b")
        style.configure("Treeview", background=self.PANEL_2, fieldbackground=self.PANEL_2, foreground=self.TEXT, rowheight=30, borderwidth=0)
        style.configure("Treeview.Heading", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#254b78")])
        style.configure("TNotebook", background=self.BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 10), background=self.PANEL, foreground=self.MUTED)
        style.map("TNotebook.Tab", background=[("selected", self.PANEL_2)], foreground=[("selected", self.TEXT)])
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=24)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="HelixGrid", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Windows Control Center", style="Subtitle.TLabel").pack(side="left", padx=(12, 0), pady=(13, 0))
        ttk.Button(header, text="Opdater", command=self.refresh_status).pack(side="right")

        cards = ttk.Frame(root)
        cards.pack(fill="x", pady=(22, 18))
        for title, variable in [
            ("Docker", self.docker_var),
            ("Coordinator", self.coordinator_var),
            ("Workers", self.worker_var),
            ("Workflows", self.workflow_var),
        ]:
            card = ttk.Frame(cards, style="Card.TFrame", padding=16)
            card.pack(side="left", fill="x", expand=True, padx=(0, 10))
            ttk.Label(card, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=variable, style="CardValue.TLabel").pack(anchor="w", pady=(5, 0))

        controls = ttk.Frame(root, style="Panel.TFrame", padding=14)
        controls.pack(fill="x", pady=(0, 16))
        ttk.Button(controls, text="▶ Start HelixGrid", style="Accent.TButton", command=self.start_cluster).pack(side="left")
        ttk.Button(controls, text="↻ Genstart", command=self.restart_cluster).pack(side="left", padx=8)
        ttk.Button(controls, text="■ Stop", style="Danger.TButton", command=self.stop_cluster).pack(side="left")
        ttk.Label(controls, textvariable=self.activity_var, style="Panel.TLabel").pack(side="right")

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        self.home_tab = ttk.Frame(notebook, padding=18)
        self.workflows_tab = ttk.Frame(notebook, padding=18)
        self.results_tab = ttk.Frame(notebook, padding=18)
        self.logs_tab = ttk.Frame(notebook, padding=18)
        notebook.add(self.home_tab, text="Filer & Backup")
        notebook.add(self.workflows_tab, text="Workflows")
        notebook.add(self.results_tab, text="Resultater")
        notebook.add(self.logs_tab, text="Logs")

        self._build_home()
        self._build_workflows()
        self._build_results()
        self._build_logs()

        footer = ttk.Frame(root)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, textvariable=self.status_var, style="Subtitle.TLabel").pack(side="left")
        ttk.Label(footer, text="Originale filer mountes read-only", style="Subtitle.TLabel").pack(side="right")

    def _build_home(self) -> None:
        settings = ttk.Frame(self.home_tab, style="Panel.TFrame", padding=18)
        settings.pack(fill="x")

        ttk.Label(settings, text="Mappe der skal arbejdes med", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).grid(row=0, column=0, sticky="w")
        workspace = ttk.Entry(settings, textvariable=self.workspace_var)
        workspace.grid(row=1, column=0, sticky="ew", pady=(7, 12), padx=(0, 8))
        ttk.Button(settings, text="Vælg mappe", command=self.choose_workspace).grid(row=1, column=1, pady=(7, 12))

        ttk.Label(settings, text="Resultatmappe", style="Panel.TLabel").grid(row=2, column=0, sticky="w")
        results = ttk.Entry(settings, textvariable=self.results_var)
        results.grid(row=3, column=0, sticky="ew", pady=(7, 12), padx=(0, 8))
        ttk.Button(settings, text="Vælg mappe", command=self.choose_results).grid(row=3, column=1, pady=(7, 12))

        row = ttk.Frame(settings, style="Panel.TFrame")
        row.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(row, text="Workers:", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(row, from_=1, to=16, textvariable=self.workers_var, width=5).pack(side="left", padx=(8, 22))
        ttk.Checkbutton(row, text="Start automatisk når dashboardet åbner", variable=self.auto_start_var, command=self.save_settings).pack(side="left")
        ttk.Button(row, text="Gem indstillinger", command=self.save_settings).pack(side="right")
        settings.columnconfigure(0, weight=1)

        actions = ttk.Frame(self.home_tab)
        actions.pack(fill="both", expand=True, pady=(18, 0))
        audit = ttk.Frame(actions, style="Card.TFrame", padding=22)
        audit.pack(side="left", fill="both", expand=True, padx=(0, 9))
        ttk.Label(audit, text="FIL-AUDIT", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(audit, text="Find dubletter og få overblik", style="CardValue.TLabel").pack(anchor="w", pady=(6, 8))
        ttk.Label(audit, text="Scanner størrelser, filtyper og checksums. Finder kun byte-identiske dubletter.", style="CardTitle.TLabel", wraplength=420).pack(anchor="w")
        ttk.Button(audit, text="Start audit", style="Accent.TButton", command=lambda: self.run_workflow("audit")).pack(anchor="w", pady=(18, 0))

        backup = ttk.Frame(actions, style="Card.TFrame", padding=22)
        backup.pack(side="left", fill="both", expand=True, padx=(9, 0))
        ttk.Label(backup, text="BACKUP", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(backup, text="Lav en komprimeret backup", style="CardValue.TLabel").pack(anchor="w", pady=(6, 8))
        ttk.Label(backup, text="Originalmappen er read-only. Backup og SHA-256 skrives i resultatmappen.", style="CardTitle.TLabel", wraplength=420).pack(anchor="w")
        ttk.Button(backup, text="Start backup", command=lambda: self.run_workflow("backup")).pack(anchor="w", pady=(18, 0))

    def _build_workflows(self) -> None:
        toolbar = ttk.Frame(self.workflows_tab)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Opdater workflows", command=self.refresh_workflows).pack(side="left")
        ttk.Button(toolbar, text="Annuller valgt", command=self.cancel_selected_workflow).pack(side="left", padx=8)

        self.workflow_tree = ttk.Treeview(self.workflows_tab, columns=("name", "state", "created"), show="headings")
        self.workflow_tree.heading("name", text="Navn")
        self.workflow_tree.heading("state", text="Status")
        self.workflow_tree.heading("created", text="Oprettet")
        self.workflow_tree.column("name", width=430)
        self.workflow_tree.column("state", width=150)
        self.workflow_tree.column("created", width=260)
        self.workflow_tree.pack(fill="both", expand=True)

    def _build_results(self) -> None:
        toolbar = ttk.Frame(self.results_tab)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Åbn resultatmappe", style="Accent.TButton", command=self.open_results).pack(side="left")
        ttk.Button(toolbar, text="Genindlæs rapport", command=self.load_summary).pack(side="left", padx=8)

        self.summary_text = Text(
            self.results_tab,
            bg=self.PANEL_2,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            font=("Cascadia Mono", 10),
            padx=16,
            pady=14,
            wrap="word",
        )
        self.summary_text.pack(fill="both", expand=True)
        self.load_summary()

    def _build_logs(self) -> None:
        toolbar = ttk.Frame(self.logs_tab)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Hent logs", command=self.refresh_logs).pack(side="left")
        ttk.Button(toolbar, text="Ryd visning", command=lambda: self._set_text(self.log_text, "")).pack(side="left", padx=8)

        self.log_text = Text(
            self.logs_tab,
            bg="#080c17",
            fg="#c7d4e9",
            insertbackground=self.TEXT,
            relief="flat",
            font=("Cascadia Mono", 9),
            padx=14,
            pady=12,
            wrap="none",
        )
        self.log_text.pack(fill="both", expand=True)

    def current_config(self) -> DashboardConfig:
        return DashboardConfig(
            workspace=self.workspace_var.get().strip(),
            results=self.results_var.get().strip(),
            workers=max(1, min(16, int(self.workers_var.get()))),
            auto_start=bool(self.auto_start_var.get()),
        )

    def save_settings(self) -> None:
        config = self.current_config()
        Path(config.workspace).mkdir(parents=True, exist_ok=True)
        Path(config.results).mkdir(parents=True, exist_ok=True)
        config.save()
        self.config_state = config
        self.status_var.set("Indstillinger gemt")

    def choose_workspace(self) -> None:
        selected = filedialog.askdirectory(title="Vælg mappe", initialdir=self.workspace_var.get() or str(Path.home()))
        if selected:
            self.workspace_var.set(selected)
            self.save_settings()

    def choose_results(self) -> None:
        selected = filedialog.askdirectory(title="Vælg resultatmappe", initialdir=self.results_var.get() or str(repo_root()))
        if selected:
            self.results_var.set(selected)
            self.save_settings()

    def _background(self, label: str, fn) -> None:
        if self._busy:
            self.status_var.set("Der kører allerede en handling")
            return
        self._busy = True
        self.activity_var.set(label)

        def runner():
            try:
                result = fn()
                self._events.put(("success", result))
            except Exception as exc:
                self._events.put(("error", exc))
            finally:
                self._events.put(("idle", None))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "success":
                if isinstance(payload, str) and payload:
                    self.status_var.set(payload)
            elif kind == "error":
                self.status_var.set(f"Fejl: {payload}")
                messagebox.showerror("HelixGrid", str(payload))
            elif kind == "idle":
                self._busy = False
                self.activity_var.set("Klar")
                self.refresh_status()
                self.refresh_workflows()
                self.load_summary()
        self.after(100, self._poll_events)

    def ensure_started(self) -> None:
        if coordinator_online():
            return
        self.start_cluster()

    def _wait_for_docker(self, seconds: int = 90) -> None:
        if docker_available():
            return
        if not launch_docker_desktop():
            raise RuntimeError("Docker Desktop blev ikke fundet. Kør windows-install.bat igen.")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if docker_available():
                return
            time.sleep(2)
        raise RuntimeError("Docker Desktop startede ikke. Åbn Docker Desktop og prøv igen.")

    def start_cluster(self) -> None:
        self.save_settings()

        def work():
            self._events.put(("status", "Starter Docker…"))
            self._wait_for_docker()
            config = self.current_config()
            run_command(
                ["docker", "compose", "up", "-d", "--build", "--scale", f"worker={config.workers}"],
                config=config,
                timeout=900,
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if coordinator_online():
                    return "HelixGrid kører"
                time.sleep(1)
            raise RuntimeError("Coordinator blev ikke klar. Se fanen Logs.")

        self._background("Starter HelixGrid…", work)

    def stop_cluster(self) -> None:
        self.save_settings()

        def work():
            config = self.current_config()
            if docker_available():
                run_command(["docker", "compose", "down"], config=config, timeout=120, check=False)
            return "HelixGrid stoppet"

        self._background("Stopper…", work)

    def restart_cluster(self) -> None:
        self.save_settings()

        def work():
            self._wait_for_docker()
            config = self.current_config()
            run_command(["docker", "compose", "down"], config=config, timeout=120, check=False)
            run_command(
                ["docker", "compose", "up", "-d", "--build", "--scale", f"worker={config.workers}"],
                config=config,
                timeout=900,
            )
            return "HelixGrid genstartet"

        self._background("Genstarter…", work)

    def run_workflow(self, mode: str) -> None:
        self.save_settings()

        def work():
            self._wait_for_docker()
            config = self.current_config()
            run_command(
                ["docker", "compose", "up", "-d", "--build", "--scale", f"worker={config.workers}"],
                config=config,
                timeout=900,
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not coordinator_online():
                time.sleep(1)
            if not coordinator_online():
                raise RuntimeError("Coordinator er ikke klar")

            filename = "windows-file-audit.json" if mode == "audit" else "windows-file-backup.json"
            payload = json.loads((repo_root() / "examples" / filename).read_text(encoding="utf-8"))
            response = api_request("POST", "/v1/workflows", payload, timeout=10)
            workflow = response["data"]
            workflow_id = workflow["id"]
            self._events.put(("workflow_started", workflow_id))
            while True:
                state = api_request("GET", f"/v1/workflows/{workflow_id}", timeout=10)["data"]
                status = state["state"]
                self._events.put(("progress", f"{payload['name']}: {status}"))
                if status in TERMINAL_STATES:
                    if status != "SUCCEEDED":
                        raise RuntimeError(f"Workflow endte som {status}")
                    return "Audit færdig" if mode == "audit" else "Backup færdig"
                time.sleep(0.8)

        self._background("Kører audit…" if mode == "audit" else "Laver backup…", work)

    def refresh_status(self) -> None:
        def probe():
            docker = docker_available()
            coordinator = coordinator_online() if docker else False
            workers = []
            workflows = []
            if coordinator:
                try:
                    workers = api_request("GET", "/v1/workers", timeout=2).get("data", [])
                    workflows = api_request("GET", "/v1/workflows", timeout=2).get("data", [])
                except Exception:
                    pass
            self._events.put(("probe", (docker, coordinator, workers, workflows)))

        threading.Thread(target=probe, daemon=True).start()

    def _periodic_refresh(self) -> None:
        self.refresh_status()
        self.after(3500, self._periodic_refresh)

    def refresh_workflows(self) -> None:
        if not coordinator_online():
            return
        try:
            workflows = api_request("GET", "/v1/workflows", timeout=3).get("data", [])
        except Exception:
            return
        self._last_workflows = workflows
        for item in self.workflow_tree.get_children():
            self.workflow_tree.delete(item)
        for workflow in workflows:
            self.workflow_tree.insert(
                "",
                "end",
                iid=workflow.get("id"),
                values=(workflow.get("name", "?"), workflow.get("state", "?"), workflow.get("created_at", "")),
            )

    def cancel_selected_workflow(self) -> None:
        selection = self.workflow_tree.selection()
        if not selection:
            messagebox.showinfo("HelixGrid", "Vælg et workflow først.")
            return
        workflow_id = selection[0]
        try:
            api_request("POST", f"/v1/workflows/{workflow_id}/cancel", {}, timeout=5)
            self.refresh_workflows()
        except Exception as exc:
            messagebox.showerror("HelixGrid", str(exc))

    def refresh_logs(self) -> None:
        self.save_settings()

        def work():
            config = self.current_config()
            result = run_command(
                ["docker", "compose", "logs", "--no-color", "--tail", "350"],
                config=config,
                timeout=20,
                check=False,
            )
            self._events.put(("logs", result.stdout + result.stderr))
            return "Logs opdateret"

        self._background("Henter logs…", work)

    def load_summary(self) -> None:
        path = Path(self.results_var.get() or self.config_state.results) / "summary.txt"
        if not path.exists():
            self._set_text(self.summary_text, "Der er endnu ingen audit-rapport.\n\nVælg en mappe og klik “Start audit”.")
            return
        try:
            self._set_text(self.summary_text, path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            self._set_text(self.summary_text, str(exc))

    def open_results(self) -> None:
        path = Path(self.results_var.get())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    @staticmethod
    def _set_text(widget: Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", END)
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _on_close(self) -> None:
        self.save_settings()
        self.destroy()

    def _handle_custom_event(self, kind: str, payload: object) -> bool:
        if kind == "probe":
            docker, coordinator, workers, workflows = payload
            self.docker_var.set("Kører" if docker else "Stoppet")
            self.coordinator_var.set("Online" if coordinator else "Offline")
            self.worker_var.set(f"{len(workers)} workers")
            self.workflow_var.set(f"{len(workflows)} workflows")
            self.status_var.set("Systemet er klar" if coordinator else "HelixGrid er ikke startet")
            return True
        if kind == "logs":
            self._set_text(self.log_text, str(payload))
            return True
        if kind == "progress":
            self.status_var.set(str(payload))
            return True
        if kind == "workflow_started":
            self.status_var.set(f"Workflow startet: {payload}")
            return True
        if kind == "status":
            self.status_var.set(str(payload))
            return True
        return False

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if self._handle_custom_event(kind, payload):
                continue
            if kind == "success":
                if isinstance(payload, str) and payload:
                    self.status_var.set(payload)
            elif kind == "error":
                self.status_var.set(f"Fejl: {payload}")
                messagebox.showerror("HelixGrid", str(payload))
            elif kind == "idle":
                self._busy = False
                self.activity_var.set("Klar")
                self.refresh_status()
                self.refresh_workflows()
                self.load_summary()
        self.after(100, self._poll_events)


def main() -> int:
    if os.name != "nt":
        print("HelixGrid Windows Dashboard requires Windows.", file=sys.stderr)
        return 2
    app = HelixDashboard()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
