package com.example.logging;

import static com.example.logging.GoogleCloudStructuredLogFormatter.SOURCE_LOCATION;
import static com.example.logging.LogCapture.nested;
import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.context.WebApplicationContext;

/**
 * The whole application with its real {@code application.yaml}: the formatter is activated from
 * configuration, and what reaches stdout during a request is single-line Cloud Logging JSON.
 *
 * <p>The project id is a test property because the formatter is built for every Spring context and
 * refuses to start without one.
 */
@SpringBootTest(properties = "google.cloud.project=context-test-project")
@ExtendWith(OutputCaptureExtension.class)
class JavaLoggingApplicationTest {

    private final MockMvc mvc;

    JavaLoggingApplicationTest(@Autowired WebApplicationContext context) {
        this.mvc = MockMvcBuilders.webAppContextSetup(context).build();
    }

    /**
     * Every non-blank stdout line must parse as JSON, or this throws. The capture reaches back to
     * the start of the class, so it also holds the startup lines and the lines of earlier tests;
     * each test therefore picks its own lines by logger.
     */
    private static List<Map<String, Object>> stdoutLines(CapturedOutput output) {
        return output.getOut()
                .lines()
                .filter(line -> !line.isBlank())
                .map(LogCapture::parse)
                .toList();
    }

    private static List<Map<String, Object>> linesLoggedBy(CapturedOutput output, Class<?> type) {
        return stdoutLines(output).stream()
                .filter(line -> type.getName().equals(line.get("logger")))
                .toList();
    }

    @Test
    void placedOrderIsAJsonLineWithItsKeyValuePairs(CapturedOutput output) throws Exception {
        mvc.perform(post("/orders").param("cartId", "cart-1001")).andExpect(status().isOk());

        assertThat(linesLoggedBy(output, OrderController.class))
                .isNotEmpty()
                .allSatisfy(
                        line -> {
                            assertThat(line)
                                    .containsEntry("severity", "INFO")
                                    .containsEntry("message", "placed order")
                                    .containsEntry("cart_id", "cart-1001")
                                    .containsEntry("total", 67.97)
                                    .containsKeys(
                                            "timestamp",
                                            "timestampSeconds",
                                            "timestampNanos",
                                            "thread",
                                            "order_id")
                                    .doesNotContainKey("tags");
                            assertThat(nested(line, SOURCE_LOCATION))
                                    .containsEntry(
                                            "function",
                                            OrderController.class.getName() + ".placeOrder");
                        });
    }

    @Test
    void unexpectedFailureIsAnErrorLineWithTheStackTraceInMessage(CapturedOutput output)
            throws Exception {
        mvc.perform(get("/boom")).andExpect(status().isInternalServerError());

        assertThat(linesLoggedBy(output, ApiExceptionHandler.class))
                .isNotEmpty()
                .allSatisfy(
                        line -> {
                            assertThat(line).containsEntry("severity", "ERROR");
                            assertThat((String) line.get("message"))
                                    .startsWith(
                                            "request failed\njava.lang.IllegalStateException:"
                                                    + " payment gateway timed out\n\tat ")
                                    .contains(
                                            "\nCaused by: java.net.SocketTimeoutException: read"
                                                    + " timed out");
                        });
    }

    @Test
    void unknownPathIs404AndIsNotLoggedAsAFailure(CapturedOutput output) throws Exception {
        mvc.perform(get("/nope")).andExpect(status().isNotFound());

        assertThat(stdoutLines(output))
                .extracting(line -> String.valueOf(line.get("message")))
                .noneMatch(message -> message.contains("NoResourceFoundException"));
    }

    @Test
    void frameworkLinesCarryNoCommonsLoggingTag(CapturedOutput output) throws Exception {
        mvc.perform(get("/healthz")).andExpect(status().isOk());

        assertThat(stdoutLines(output))
                .allSatisfy(line -> assertThat(line).doesNotContainKey("tags"));
    }
}
