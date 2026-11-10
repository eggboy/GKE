package com.example.logging;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/** Minimal Spring Boot service that shows the formatter at work. */
@SpringBootApplication
public class JavaLoggingApplication {

    public static void main(String[] args) {
        SpringApplication.run(JavaLoggingApplication.class, args);
    }
}
