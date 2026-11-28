# POC Go (F-002) : base de compilation épinglée par digest (BR-005) ; image finale : debian trixie-slim, préexistante sur la machine.
FROM golang@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -o /gateway .

FROM debian@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
COPY --from=build /gateway /gateway
ENTRYPOINT ["/gateway"]
