FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags='-s -w' -o /out/helixd ./cmd/helixd

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/helixd /helixd
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/helixd"]
