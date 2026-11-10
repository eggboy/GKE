# All Things GKE

Projects I built for developers and platform operators who run services on Google Kubernetes Engine. Each directory holds one project with its own README and Kubernetes manifests.

## Projects

| Project | What it shows | Stack |
| :--- | :--- | :--- |
| [`glb-trace-adk`](glb-trace-adk/) | One Cloud Trace waterfall across a Global External ALB, a Google ADK agent, a Regional Internal ALB and a FastAPI backend, built on the Cloud Load Balancing distributed tracing Preview and GKE Managed OpenTelemetry. Eight scenarios (outage, latency, WAF deny, sampling, log correlation) with screenshots in [`docs/DEMO_RESULTS.md`](glb-trace-adk/docs/DEMO_RESULTS.md). | Python 3.12, FastAPI, Google ADK 2.x, Gateway API, Cloud Armor, Cloud Build |
| [`logging-best-practice/java-logging`](logging-best-practice/java-logging/) | A Spring Boot `StructuredLogFormatter` that writes one Cloud Logging JSON document per event, stack trace inside the line, with `trace`, `spanId` and `traceSampled` taken from the OpenTelemetry Java agent. Covers the traps (double ingestion, JVM noise on stderr) and ships a vendor-neutral formatter for AKS, EKS or Loki, see [`PORTABILITY.md`](logging-best-practice/java-logging/PORTABILITY.md). | Java 21, Spring Boot 4.1, Maven, OpenTelemetry Java agent 2.22 |
| [`logging-best-practice/python-logging`](logging-best-practice/python-logging/) | The same contract for FastAPI: a `logging.Formatter` that keeps tracebacks inside the line, drops uvicorn's health-probe access lines and stays trace-correlated behind a `QueueHandler`. | Python 3.12, FastAPI, uvicorn, `opentelemetry-instrument` |

## Related

[`eggboy/gke-adk-otel`](https://github.com/eggboy/gke-adk-otel): the ADK plus GKE Managed OpenTelemetry reference that `storefront-agent` follows.
