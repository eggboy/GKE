# syntax=docker/dockerfile:1
# Build stage: Maven + JDK 21. Pin by digest in production.
FROM maven:3.9-eclipse-temurin-21 AS builder
WORKDIR /app
COPY pom.xml ./
RUN mvn -q -B dependency:go-offline
COPY src ./src
RUN mvn -q -B -DskipTests package

# The OpenTelemetry Java agent reads the OTEL_* variables injected by the GKE Instrumentation CR, instruments
# Tomcat/Spring MVC/HTTP clients and puts trace_id/span_id/trace_flags into the Logback MDC that the formatter
# reads. Pin a version you have tested.
ARG OTEL_AGENT_VERSION=2.22.0
ADD https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/download/v${OTEL_AGENT_VERSION}/opentelemetry-javaagent.jar /app/opentelemetry-javaagent.jar
# ADD from a URL writes the file as root with mode 0600. The runtime stage runs as appuser, and a JVM that cannot
# read its -javaagent jar exits at startup with "Error opening zip file or JAR manifest missing".
RUN chmod 0644 /app/opentelemetry-javaagent.jar

FROM eclipse-temurin:21-jre
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /app/target/java-logging-*.jar /app/app.jar
COPY --from=builder /app/opentelemetry-javaagent.jar /app/opentelemetry-javaagent.jar
# OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_PROTOCOL and the sampler are injected by the GKE Instrumentation
# CR and take precedence over these image defaults, which only matter for plain `docker run`.
#   OTEL_JAVAAGENT_LOGGING=none          the agent's own banner would otherwise be plain text on stderr (= ERROR entries)
#   OTEL_LOGS_EXPORTER=none              stdout JSON is the single log path; stops the upstream agent's OTLP log export.
#                                        If you swap in Azure's applicationinsights-agent.jar, also set
#                                        APPLICATIONINSIGHTS_INSTRUMENTATION_LOGGING_LEVEL=OFF (it ignores OTEL_LOGS_EXPORTER).
ENV OTEL_SERVICE_NAME="java-logging" \
    OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf" \
    OTEL_JAVAAGENT_LOGGING="none" \
    OTEL_LOGS_EXPORTER="none"
USER appuser
EXPOSE 8080
# The JVM flags sit on the command line, not in JAVA_TOOL_OPTIONS: the JVM announces that variable with a plain-text
# "Picked up JAVA_TOOL_OPTIONS: ..." line on stderr at every start, and GKE ingests stderr text as ERROR entries.
#   -javaagent:/app/...                  on EKS/AKS pair with plain OTEL_* env vars or OpenTelemetry Operator
#                                        `inject-sdk: "true"` (do not use `inject-java: "true"`, which mounts and
#                                        attaches a second Java agent through JAVA_TOOL_OPTIONS).
#   -Xshare:off                          the agent appends to the bootstrap class path; with class data sharing on, the
#                                        JVM prints "warning: Sharing is only supported for boot loader classes" on stderr
#   -Xlog:disable                        GC logs and other JVM unified logging are plain text
#   -XX:+ExitOnOutOfMemoryError          restart the Pod instead of logging OOM forever
ENTRYPOINT ["java", "-javaagent:/app/opentelemetry-javaagent.jar", "-Xshare:off", "-Xlog:disable", "-XX:+ExitOnOutOfMemoryError", "-jar", "/app/app.jar"]
