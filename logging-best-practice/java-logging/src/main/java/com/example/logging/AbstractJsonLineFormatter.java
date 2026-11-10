package com.example.logging;

import ch.qos.logback.classic.pattern.ThrowableProxyConverter;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.IThrowableProxy;
import ch.qos.logback.classic.spi.ThrowableProxy;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.Charset;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.Predicate;
import org.slf4j.Marker;
import org.slf4j.event.KeyValuePair;
import org.springframework.boot.json.JsonWriter;
import org.springframework.boot.json.JsonWriter.ValueProcessor;
import org.springframework.boot.logging.StackTracePrinter;
import org.springframework.boot.logging.structured.JsonWriterStructuredLogFormatter;
import org.springframework.boot.logging.structured.StructuredLoggingJsonMembersCustomizer;
import org.springframework.core.env.Environment;
import org.springframework.util.StringUtils;

/**
 * Mechanics shared by {@link GoogleCloudStructuredLogFormatter} and {@link
 * OpenTelemetryStructuredLogFormatter}: one JSON document per line, the stack trace appended to
 * {@code message}, MDC entries and SLF4J key-value pairs merged into top-level fields.
 *
 * <p>Three things Spring Boot's {@code JsonWriter} does not do are handled here:
 *
 * <ul>
 *   <li>It refuses to write a member name twice and throws. Logback swallows that exception, so the
 *       line would never reach stdout. {@link #format} and {@link #formatAsBytes} catch it and
 *       write the core members plus {@value #FORMAT_ERROR} instead.
 *   <li>It prints numbers with {@code toString()}, so {@code NaN} and {@code Infinity} would make
 *       the line invalid JSON. They are written as strings.
 *   <li>It does not bound the message. {@value #MESSAGE_MAX_LENGTH_PROPERTY} does; the stack trace
 *       keeps its own limits in {@code logging.structured.json.stacktrace.*}.
 * </ul>
 */
abstract class AbstractJsonLineFormatter extends JsonWriterStructuredLogFormatter<ILoggingEvent> {

    /** Maximum characters of the formatted message, stack trace excluded; 0 disables the cap. */
    static final String MESSAGE_MAX_LENGTH_PROPERTY = "app.logging.message-max-length";

    static final int DEFAULT_MESSAGE_MAX_LENGTH = 65_536;

    /** Present only on a fallback line: why the full document could not be written. */
    static final String FORMAT_ERROR = "log_format_error";

    /** SLF4J marker names, as in Spring Boot's {@code logstash} format. */
    static final String TAGS = "tags";

    /**
     * Apache Commons Logging 1.3 puts this marker on every event it bridges to SLF4J, which is
     * every line Spring Framework 7 logs. It says nothing about the event, so it is not a tag.
     */
    static final String COMMONS_LOGGING_MARKER = "COMMONS-LOGGING";

    private final JsonWriter<FormatFailure> fallbackWriter;

    /**
     * Builds the writer for the full document and the one for the fallback line.
     *
     * @param coreMembers timestamp, level, message, logger and thread; also the fallback line
     * @param schemaMembers everything else the concrete schema writes
     * @param customizer Spring Boot's {@code include/exclude/rename/add} customizer, or {@code
     *     null}
     */
    AbstractJsonLineFormatter(
            Consumer<JsonWriter.Members<ILoggingEvent>> coreMembers,
            Consumer<JsonWriter.Members<ILoggingEvent>> schemaMembers,
            StructuredLoggingJsonMembersCustomizer<?> customizer) {
        super(
                members -> {
                    coreMembers.accept(members);
                    schemaMembers.accept(members);
                    members.applyingValueProcessor(
                            ValueProcessor.of(AbstractJsonLineFormatter::jsonSafe));
                },
                customizer);
        this.fallbackWriter =
                JsonWriter.<FormatFailure>of(
                                members -> {
                                    members.from(FormatFailure::event).usingMembers(coreMembers);
                                    members.add(
                                            FORMAT_ERROR, failure -> failure.cause().toString());
                                })
                        .withNewLineAtEnd();
    }

    @Override
    public final String format(ILoggingEvent event) {
        try {
            return super.format(event);
        } catch (RuntimeException ex) {
            // Last resort on purpose: whatever broke, a degraded line beats a silently lost one.
            return this.fallbackWriter.writeToString(new FormatFailure(event, ex));
        }
    }

    @Override
    public final byte[] formatAsBytes(ILoggingEvent event, Charset charset) {
        try {
            return super.formatAsBytes(event, charset);
        } catch (RuntimeException ex) {
            // Same last resort as format(); Spring's encoder calls this variant.
            return this.fallbackWriter
                    .writeToString(new FormatFailure(event, ex))
                    .getBytes(charset);
        }
    }

    /** The five members both schemas share; only the name and spelling of the level differ. */
    static Consumer<JsonWriter.Members<ILoggingEvent>> coreMembers(
            String levelName, Function<ILoggingEvent, String> level, MessageRenderer message) {
        return members -> {
            members.add("timestamp", ILoggingEvent::getInstant);
            members.add(levelName, event -> level.apply(event));
            members.add("message", event -> message.render(event));
            members.add("logger", ILoggingEvent::getLoggerName);
            members.add("thread", ILoggingEvent::getThreadName);
        };
    }

    static MessageRenderer messageRenderer(
            Environment environment,
            StackTracePrinter stackTracePrinter,
            ThrowableProxyConverter throwableProxyConverter) {
        int maxLength =
                environment.getProperty(
                        MESSAGE_MAX_LENGTH_PROPERTY, Integer.class, DEFAULT_MESSAGE_MAX_LENGTH);
        return new MessageRenderer(stackTracePrinter, throwableProxyConverter, maxLength);
    }

    /**
     * MDC entries first, then SLF4J key-value pairs, in one map: a key-value pair replaces an MDC
     * entry of the same name, and a name can never be written twice.
     */
    static Map<String, Object> userFields(ILoggingEvent event, Predicate<String> userKey) {
        Map<String, String> mdc = event.getMDCPropertyMap();
        List<KeyValuePair> keyValues = event.getKeyValuePairs();
        if ((mdc == null || mdc.isEmpty()) && (keyValues == null || keyValues.isEmpty())) {
            return Map.of();
        }
        Map<String, Object> fields = new LinkedHashMap<>();
        if (mdc != null) {
            mdc.forEach(
                    (key, value) -> {
                        if (userKey.test(key)) {
                            fields.put(key, value);
                        }
                    });
        }
        if (keyValues != null) {
            for (KeyValuePair pair : keyValues) {
                if (pair != null && userKey.test(pair.key)) {
                    fields.put(pair.key, pair.value);
                }
            }
        }
        return fields;
    }

    /** A user key is non-empty, does not start with {@code _}, and is not a reserved name. */
    static boolean isUserKey(String key, Set<String> reserved) {
        return StringUtils.hasLength(key) && !key.startsWith("_") && !reserved.contains(key);
    }

    /**
     * Marker names including referenced markers, in the order they were added, without the Commons
     * Logging bridge marker.
     */
    static Set<String> tags(ILoggingEvent event) {
        List<Marker> markers = event.getMarkerList();
        if (markers == null || markers.isEmpty()) {
            return Set.of();
        }
        Set<String> names = new LinkedHashSet<>();
        markers.forEach(marker -> collect(marker, names));
        names.remove(COMMONS_LOGGING_MARKER);
        return names;
    }

    private static void collect(Marker marker, Set<String> names) {
        if (names.add(marker.getName())) {
            marker.iterator().forEachRemaining(reference -> collect(reference, names));
        }
    }

    /** JSON has no literal for NaN or Infinity; a string keeps the line parseable. */
    private static Object jsonSafe(Object value) {
        return switch (value) {
            case Double number when !Double.isFinite(number) -> number.toString();
            case Float number when !Float.isFinite(number) -> number.toString();
            case null, default -> value;
        };
    }

    /**
     * Writes the {@code message} member: the bounded text, then the stack trace after a newline.
     */
    record MessageRenderer(
            StackTracePrinter stackTracePrinter,
            ThrowableProxyConverter throwableProxyConverter,
            int maxLength) {

        String render(ILoggingEvent event) {
            String text = truncate(event.getFormattedMessage());
            IThrowableProxy proxy = event.getThrowableProxy();
            if (proxy == null) {
                return text;
            }
            String stackTrace = stackTrace(event, proxy).stripTrailing();
            return text.isEmpty() ? stackTrace : text + "\n" + stackTrace;
        }

        private String truncate(String text) {
            if (text == null) {
                return "";
            }
            if (maxLength <= 0 || text.length() <= maxLength) {
                return text;
            }
            // Never cut a surrogate pair in half.
            int end =
                    Character.isHighSurrogate(text.charAt(maxLength - 1))
                            ? maxLength - 1
                            : maxLength;
            return text.substring(0, end) + "... [" + (text.length() - end) + " more characters]";
        }

        private String stackTrace(ILoggingEvent event, IThrowableProxy proxy) {
            if (stackTracePrinter != null
                    && proxy instanceof ThrowableProxy throwableProxy
                    && throwableProxy.getThrowable() != null) {
                StringBuilder out = new StringBuilder();
                try {
                    stackTracePrinter.printStackTrace(throwableProxy.getThrowable(), out);
                } catch (IOException ex) {
                    throw new UncheckedIOException(ex); // StringBuilder never throws
                }
                return out.toString();
            }
            return throwableProxyConverter.convert(event);
        }
    }

    private record FormatFailure(ILoggingEvent event, RuntimeException cause) {}
}
