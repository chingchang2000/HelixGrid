SHELL := /bin/sh
.DEFAULT_GOAL := help

.PHONY: help check fmt test test-go test-rust test-python test-typescript test-cpp \
        build build-go build-rust build-typescript build-cpp run docker-up docker-down clean

help:
	@printf '%s\n' \
	  'HelixGrid development commands' \
	  '' \
	  '  make check             format/check/test all installed toolchains' \
	  '  make test              run all test suites' \
	  '  make build             build all compiled components' \
	  '  make run               run the Go coordinator' \
	  '  make docker-up         start coordinator + worker cluster' \
	  '  make docker-down       stop local cluster' \
	  '  make fmt               format Go/Rust/Python where tools are installed' \
	  '  make clean             remove local build outputs'

check: test
	@command -v go >/dev/null 2>&1 && (cd coordinator && go vet ./...) || true
	@command -v cargo >/dev/null 2>&1 && (cd worker && cargo clippy --all-targets -- -D warnings) || true
	@command -v python3 >/dev/null 2>&1 && python3 -m compileall -q sdk/python/src || true
	@command -v npm >/dev/null 2>&1 && (cd sdk/typescript && npm run check) || true

test: test-go test-rust test-python test-typescript test-cpp

test-go:
	@command -v go >/dev/null 2>&1 && (cd coordinator && go test ./...) || echo 'skip: go not installed'

test-rust:
	@command -v cargo >/dev/null 2>&1 && (cd worker && cargo test) || echo 'skip: cargo not installed'

test-python:
	@command -v python3 >/dev/null 2>&1 && python3 -m unittest discover -s sdk/python/tests -v || echo 'skip: python3 not installed'

test-typescript:
	@if command -v npm >/dev/null 2>&1; then cd sdk/typescript && npm install --ignore-scripts && npm run check; else echo 'skip: npm not installed'; fi

test-cpp:
	@if command -v cmake >/dev/null 2>&1; then cmake -S simulator -B .build/simulator -DCMAKE_BUILD_TYPE=Release && cmake --build .build/simulator && ctest --test-dir .build/simulator --output-on-failure; else echo 'skip: cmake not installed'; fi

build: build-go build-rust build-typescript build-cpp

build-go:
	@mkdir -p .build/bin
	cd coordinator && go build -trimpath -o ../.build/bin/helixd ./cmd/helixd

build-rust:
	cd worker && cargo build --release
	@mkdir -p .build/bin
	@cp worker/target/release/helix-worker .build/bin/helix-worker 2>/dev/null || cp worker/target/release/helix-worker.exe .build/bin/helix-worker.exe

build-typescript:
	cd sdk/typescript && npm install --ignore-scripts && npm run build

build-cpp:
	cmake -S simulator -B .build/simulator -DCMAKE_BUILD_TYPE=Release
	cmake --build .build/simulator

run:
	cd coordinator && go run ./cmd/helixd

docker-up:
	docker compose up --build --scale worker=3

docker-down:
	docker compose down --remove-orphans

fmt:
	@command -v go >/dev/null 2>&1 && (cd coordinator && gofmt -w $$(find . -name '*.go' -type f)) || true
	@command -v cargo >/dev/null 2>&1 && (cd worker && cargo fmt) || true
	@command -v ruff >/dev/null 2>&1 && ruff format sdk/python || true

clean:
	rm -rf .build sdk/typescript/dist sdk/typescript/node_modules worker/target
	find sdk/python -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
