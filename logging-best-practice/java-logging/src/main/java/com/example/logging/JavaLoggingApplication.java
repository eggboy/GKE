package com.example.logging;

import org.slf4j.LoggerFactory;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/** Minimal Spring Boot service that shows the formatter at work. */
@SpringBootApplication
public class JavaLoggingApplication {

    public static void main(String[] args) {
        logUncaughtExceptions();
        SpringApplication.run(JavaLoggingApplication.class, args);
    }

    /**
     * Sends an exception that ends a thread through the logger. The JVM's own handler prints {@code
     * Exception in thread "..."} and the stack trace to stderr as plain text, one Cloud Logging
     * entry per frame. Spring's own threads (request handling, {@code @Async}, {@code @Scheduled})
     * log their failures already; this covers every other thread.
     */
    static void logUncaughtExceptions() {
        Thread.setDefaultUncaughtExceptionHandler(
                (thread, ex) ->
                        LoggerFactory.getLogger(JavaLoggingApplication.class)
                                .error("uncaught exception in thread {}", thread.getName(), ex));
    }
}
