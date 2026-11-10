package com.example.logging;

import static com.example.logging.SpanQueryRedactionFilter.URL_QUERY;
import static org.assertj.core.api.Assertions.assertThat;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

/**
 * The filter against a span of the real SDK: what the agent would have recorded as {@code
 * url.query} is replaced before the span ends.
 */
class SpanQueryRedactionFilterTest {

    private static final String RAW = "cartId=cart-1001&access_token=review-canary";

    private final SpanQueryRedactionFilter filter = new SpanQueryRedactionFilter();

    private final ApplicationContextRunner application =
            new ApplicationContextRunner().withUserConfiguration(SpanQueryRedactionFilter.class);

    @Test
    void filterIsRegisteredWhenThePropertyIsNotSet() {
        application.run(
                context -> assertThat(context).hasSingleBean(SpanQueryRedactionFilter.class));
    }

    @Test
    void filterIsRegisteredWhenThePropertyIsTrue() {
        application
                .withPropertyValues(SpanQueryRedactionFilter.ENABLED_PROPERTY + "=true")
                .run(context -> assertThat(context).hasSingleBean(SpanQueryRedactionFilter.class));
    }

    @Test
    void filterIsNotRegisteredWhenThePropertyIsFalse() {
        application
                .withPropertyValues(SpanQueryRedactionFilter.ENABLED_PROPERTY + "=false")
                .run(
                        context ->
                                assertThat(context)
                                        .doesNotHaveBean(SpanQueryRedactionFilter.class));
    }

    @ParameterizedTest
    @CsvSource({
        "cartId=cart-1001&access_token=review-canary, cartId=REDACTED&access_token=REDACTED",
        "a=, a=REDACTED",
        "a=1=2, a=REDACTED",
        "signed-token-without-a-name, REDACTED",
        "a=1&&b=2, a=REDACTED&&b=REDACTED",
    })
    void queryKeepsParameterNamesAndLosesTheirValues(String query, String redacted) {
        assertThat(SpanQueryRedactionFilter.redact(query)).isEqualTo(redacted);
    }

    @Test
    void queryOfTheRequestIsRedactedOnTheCurrentSpan() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/orders");
        request.setQueryString(RAW);
        MockFilterChain chain = new MockFilterChain();

        ReadableSpan span = inServerSpan(RAW, () -> filter.doFilter(request, response(), chain));

        assertThat(span.getAttribute(AttributeKey.stringKey(URL_QUERY)))
                .isEqualTo("cartId=REDACTED&access_token=REDACTED");
        assertThat(chain.getRequest()).as("the request went on down the chain").isSameAs(request);
    }

    @Test
    void requestWithoutAQueryLeavesTheSpanAlone() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/boom");
        MockFilterChain chain = new MockFilterChain();

        ReadableSpan span = inServerSpan(null, () -> filter.doFilter(request, response(), chain));

        assertThat(span.getAttribute(AttributeKey.stringKey(URL_QUERY))).isNull();
        assertThat(chain.getRequest()).isSameAs(request);
    }

    @Test
    void withoutACurrentSpanTheRequestStillGoesThrough() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/orders");
        request.setQueryString(RAW);
        MockFilterChain chain = new MockFilterChain();

        filter.doFilter(request, response(), chain);

        assertThat(chain.getRequest()).isSameAs(request);
    }

    private static MockHttpServletResponse response() {
        return new MockHttpServletResponse();
    }

    /**
     * Runs {@code action} inside a span that carries {@code url.query} the way the agent sets it.
     */
    private static ReadableSpan inServerSpan(String query, ThrowingRunnable action)
            throws Exception {
        Ended ended = new Ended();
        try (SdkTracerProvider provider =
                SdkTracerProvider.builder().addSpanProcessor(ended).build()) {
            Span span = provider.get("tests").spanBuilder("POST /orders").startSpan();
            if (query != null) {
                span.setAttribute(URL_QUERY, query);
            }
            try (Scope ignored = span.makeCurrent()) {
                action.run();
            } finally {
                span.end();
            }
        }
        assertThat(ended.spans).hasSize(1);
        return ended.spans.getFirst();
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    private static final class Ended implements SpanProcessor {

        private final List<ReadableSpan> spans = new ArrayList<>();

        @Override
        public void onStart(Context parentContext, ReadWriteSpan span) {}

        @Override
        public boolean isStartRequired() {
            return false;
        }

        @Override
        public void onEnd(ReadableSpan span) {
            spans.add(span);
        }

        @Override
        public boolean isEndRequired() {
            return true;
        }
    }
}
