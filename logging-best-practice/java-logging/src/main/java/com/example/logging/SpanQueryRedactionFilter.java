package com.example.logging;

import io.opentelemetry.api.trace.Span;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Arrays;
import java.util.stream.Collectors;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * Keeps query-string values out of the server span.
 *
 * <p>The OpenTelemetry Java agent records the query string of every request as {@code url.query} on
 * the server span, and its redaction setting covers client spans only. A request to {@code
 * /orders?access_token=...} therefore lands in Cloud Trace as typed. This filter runs first in the
 * chain, where the current span is the server span, and writes the attribute again: every parameter
 * keeps its name and loses its value.
 *
 * <p>A request that Tomcat rejects before the filter chain, a malformed request line for one, keeps
 * its query string. Without the agent there is no current span and the filter does nothing.
 *
 * <p>On unless {@value #ENABLED_PROPERTY} is {@code false}; the filter is then not registered at
 * all and the span keeps the query string as the agent recorded it.
 */
@Component
@ConditionalOnProperty(
        name = SpanQueryRedactionFilter.ENABLED_PROPERTY,
        havingValue = "true",
        matchIfMissing = true)
@Order(Ordered.HIGHEST_PRECEDENCE)
class SpanQueryRedactionFilter extends OncePerRequestFilter {

    /** {@code false} switches the redaction off. */
    static final String ENABLED_PROPERTY = "app.tracing.redact-url-query";

    static final String URL_QUERY = "url.query";
    static final String REDACTED = "REDACTED";

    @Override
    protected void doFilterInternal(
            HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        String query = request.getQueryString();
        if (StringUtils.hasLength(query)) {
            // Before the request is handled, so the attribute is right however the request ends.
            Span.current().setAttribute(URL_QUERY, redact(query));
        }
        chain.doFilter(request, response);
    }

    /**
     * Turns {@code a=1&b=2} into {@code a=REDACTED&b=REDACTED}; a part without a name is redacted
     * whole.
     */
    static String redact(String query) {
        return Arrays.stream(query.split("&", -1))
                .map(SpanQueryRedactionFilter::redactPair)
                .collect(Collectors.joining("&"));
    }

    private static String redactPair(String pair) {
        if (pair.isEmpty()) {
            return pair;
        }
        int equals = pair.indexOf('=');
        return (equals < 0) ? REDACTED : pair.substring(0, equals + 1) + REDACTED;
    }
}
