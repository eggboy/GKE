package com.example.logging;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.AsyncAppender;
import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.Appender;
import ch.qos.logback.core.OutputStreamAppender;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.boot.logging.logback.StructuredLogEncoder;
import org.springframework.core.env.Environment;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * Test fixture: wires a formatter the way Spring Boot does ({@code StructuredLogEncoder} plus the
 * {@code Environment} stored in the Logback context) and captures what it writes.
 *
 * <p>Every line is parsed with strict duplicate detection, so a name written twice or a token that
 * is not JSON fails the test that produced it.
 */
final class LogCapture implements AutoCloseable {

    private static final ObjectMapper STRICT_JSON =
            JsonMapper.builder().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).build();
    private static final String ENVIRONMENT_KEY = Environment.class.getName();

    private final LoggerContext context = (LoggerContext) LoggerFactory.getILoggerFactory();
    private final Object previousEnvironment = context.getObject(ENVIRONMENT_KEY);
    private final ByteArrayOutputStream stream = new ByteArrayOutputStream();
    private final CapturingAppender appender = new CapturingAppender();
    private final Logger logger;
    private Appender<ILoggingEvent> attached = appender;
    private AsyncAppender asyncAppender;

    private LogCapture(Class<?> formatter) {
        this.logger = context.getLogger("tests." + formatter.getSimpleName());
    }

    /**
     * Starts capturing through {@code formatter}. Whatever the formatter's constructor throws comes
     * out of this method, wrapped by Spring's instantiator.
     */
    static LogCapture start(Class<?> formatter, Environment environment) {
        LogCapture capture = new LogCapture(formatter);
        try {
            capture.open(formatter, environment);
        } catch (RuntimeException ex) {
            capture.close();
            throw ex;
        }
        return capture;
    }

    private void open(Class<?> formatter, Environment environment) {
        context.putObject(ENVIRONMENT_KEY, environment);
        StructuredLogEncoder encoder = new StructuredLogEncoder();
        encoder.setContext(context);
        encoder.setFormat(formatter.getName());
        encoder.start();
        appender.setContext(context);
        appender.setEncoder(encoder);
        appender.setOutputStream(stream);
        appender.start();
        logger.setAdditive(false);
        logger.setLevel(Level.DEBUG);
        logger.addAppender(attached);
    }

    /** Puts Logback's {@code AsyncAppender} in front, so formatting runs on its worker thread. */
    LogCapture behindAsyncAppender(boolean includeCallerData) {
        logger.detachAppender(attached);
        asyncAppender = new AsyncAppender();
        asyncAppender.setContext(context);
        asyncAppender.setIncludeCallerData(includeCallerData);
        asyncAppender.addAppender(appender);
        asyncAppender.start();
        attached = asyncAppender;
        logger.addAppender(attached);
        return this;
    }

    Logger logger() {
        return logger;
    }

    /** Events in the order the formatter saw them. */
    List<ILoggingEvent> events() {
        flush();
        return List.copyOf(appender.events);
    }

    /** The one line written since the last call, without its trailing newline. */
    String line() {
        flush();
        String written = stream.toString(StandardCharsets.UTF_8);
        stream.reset();
        assertThat(written).as("a newline-terminated line").endsWith("\n");
        String line = written.substring(0, written.length() - 1);
        assertThat(line).as("log agents need one JSON document per line").doesNotContain("\n");
        return line;
    }

    /** The one line written since the last call, parsed. */
    Map<String, Object> json() {
        return parse(line());
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> parse(String line) {
        return STRICT_JSON.readValue(line, Map.class);
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> nested(Map<String, Object> payload, String key) {
        return (Map<String, Object>) payload.get(key);
    }

    private void flush() {
        if (asyncAppender != null && asyncAppender.isStarted()) {
            asyncAppender.stop(); // drains the queue on the worker thread
        }
    }

    @Override
    public void close() {
        MDC.clear();
        logger.detachAppender(attached);
        flush();
        appender.stop();
        if (previousEnvironment != null) {
            context.putObject(ENVIRONMENT_KEY, previousEnvironment);
        } else {
            context.removeObject(ENVIRONMENT_KEY);
        }
    }

    private static final class CapturingAppender extends OutputStreamAppender<ILoggingEvent> {

        private final List<ILoggingEvent> events = new ArrayList<>();

        @Override
        protected void append(ILoggingEvent event) {
            events.add(event);
            super.append(event);
        }
    }
}
