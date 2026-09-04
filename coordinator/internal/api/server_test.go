package api

import (
	"bytes"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/chingchang2000/app/coordinator/internal/core"
)

func testServer() (*Server, *core.Store) {
	store := core.NewStore(core.NewEventBus(1000))
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	return New(store, logger), store
}

func TestCreateWorkflowRejectsTrailingJSON(t *testing.T) {
	server, _ := testServer()
	body := []byte(`{"name":"x","tasks":[{"id":"a","command":["true"]}]} {"extra":true}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/workflows", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	server.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestRegisterWorkerRejectsInvalidCapacityAndVersion(t *testing.T) {
	server, _ := testServer()
	cases := []string{
		`{"name":"worker","version":"1","capacity":0}`,
		`{"name":"worker","version":"1","capacity":257}`,
		`{"name":"worker","version":"","capacity":1}`,
	}
	for _, body := range cases {
		req := httptest.NewRequest(http.MethodPost, "/v1/workers/register", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		server.Handler().ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body=%s status=%d response=%s", body, rec.Code, rec.Body.String())
		}
	}
}

func TestCreateWorkflowRejectsOversizedIdempotencyKey(t *testing.T) {
	server, _ := testServer()
	body := `{"name":"x","tasks":[{"id":"a","command":["true"]}]}`
	req := httptest.NewRequest(http.MethodPost, "/v1/workflows", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", strings.Repeat("x", 513))
	rec := httptest.NewRecorder()

	server.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestWorkflowEventsRejectsInvalidLastEventID(t *testing.T) {
	server, store := testServer()
	workflow, _, err := store.CreateWorkflow(core.WorkflowSpec{
		Name: "events",
		Tasks: []core.TaskSpec{{ID: "a", Command: []string{"true"}}},
	}, "")
	if err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/workflows/"+workflow.ID+"/events", nil)
	req.Header.Set("Last-Event-ID", "-1")
	rec := httptest.NewRecorder()
	server.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}
