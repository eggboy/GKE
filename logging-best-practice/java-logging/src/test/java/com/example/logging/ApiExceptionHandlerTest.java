package com.example.logging;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

/** Status codes and log levels: a client mistake is neither a 500 nor an ERROR line. */
class ApiExceptionHandlerTest {

    private final ListAppender<ILoggingEvent> events = new ListAppender<>();
    private final Logger handlerLogger =
            (Logger) LoggerFactory.getLogger(ApiExceptionHandler.class);
    private final MockMvc mvc =
            MockMvcBuilders.standaloneSetup(new OrderController())
                    .setControllerAdvice(new ApiExceptionHandler())
                    .build();

    @BeforeEach
    void captureHandlerLog() {
        events.start();
        handlerLogger.setAdditive(false);
        handlerLogger.addAppender(events);
    }

    @AfterEach
    void releaseHandlerLog() {
        handlerLogger.detachAppender(events);
        handlerLogger.setAdditive(true);
        events.stop();
    }

    @ParameterizedTest(name = "{0} {1} -> {2}")
    @CsvSource({"POST, /orders, 400", "GET, /orders, 405", "GET, /nope, 404"})
    void clientMistakesKeepTheirStatusAndLogNoError(String method, String path, int expected)
            throws Exception {
        mvc.perform(request(HttpMethod.valueOf(method), path)).andExpect(status().is(expected));

        assertThat(events.list).extracting(ILoggingEvent::getLevel).doesNotContain(Level.ERROR);
    }

    @Test
    void unacceptableMediaTypeIs406AndLogsNoError() throws Exception {
        mvc.perform(post("/orders").param("cartId", "cart-1001").accept(MediaType.TEXT_PLAIN))
                .andExpect(status().isNotAcceptable());

        assertThat(events.list).extracting(ILoggingEvent::getLevel).doesNotContain(Level.ERROR);
    }

    @Test
    void unknownCartIs404WithAWarningThatCarriesTheCartId() throws Exception {
        mvc.perform(post("/orders").param("cartId", "cart-9")).andExpect(status().isNotFound());

        assertThat(events.list)
                .singleElement()
                .satisfies(
                        event -> {
                            assertThat(event.getLevel()).isEqualTo(Level.WARN);
                            assertThat(event.getFormattedMessage()).isEqualTo("unknown cart");
                            assertThat(event.getThrowableProxy()).isNull();
                            assertThat(event.getKeyValuePairs())
                                    .singleElement()
                                    .satisfies(
                                            pair -> {
                                                assertThat(pair.key).isEqualTo("cart_id");
                                                assertThat(pair.value).isEqualTo("cart-9");
                                            });
                        });
    }

    @Test
    void unexpectedFailureIs500WithOneErrorThatCarriesTheException() throws Exception {
        mvc.perform(get("/boom")).andExpect(status().isInternalServerError());

        assertThat(events.list)
                .singleElement()
                .satisfies(
                        event -> {
                            assertThat(event.getLevel()).isEqualTo(Level.ERROR);
                            assertThat(event.getFormattedMessage()).isEqualTo("request failed");
                            assertThat(event.getThrowableProxy().getClassName())
                                    .isEqualTo(IllegalStateException.class.getName());
                        });
    }
}
