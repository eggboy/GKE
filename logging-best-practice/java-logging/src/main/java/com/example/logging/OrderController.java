package com.example.logging;

import java.math.BigDecimal;
import java.net.SocketTimeoutException;
import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** Three endpoints: a happy path with structured fields, a 404, and an unhandled exception. */
@RestController
class OrderController {

    private static final Logger logger = LoggerFactory.getLogger(OrderController.class);
    private static final BigDecimal FLAT_TOTAL = new BigDecimal("67.97");

    record Order(String orderId, String cartId, BigDecimal total) {}

    /** Raised when a requested shopping cart ID does not exist. */
    static final class UnknownCartException extends RuntimeException {
        private final String cartId;

        UnknownCartException(String cartId) {
            super("Unknown cart: " + cartId);
            this.cartId = cartId;
        }

        String cartId() {
            return cartId;
        }
    }

    /** Unauthenticated liveness endpoint for probes. */
    @GetMapping("/healthz")
    Map<String, String> healthz() {
        return Map.of("status", "ok");
    }

    /** SLF4J key-value pairs become top-level JSON fields. */
    @PostMapping("/orders")
    Order placeOrder(@RequestParam String cartId) {
        if ("cart-9".equals(cartId)) {
            throw new UnknownCartException(cartId);
        }
        var order =
                new Order(
                        "ord-" + UUID.randomUUID().toString().substring(0, 8), cartId, FLAT_TOTAL);
        logger.atInfo()
                .addKeyValue("order_id", order.orderId())
                .addKeyValue("cart_id", order.cartId())
                .addKeyValue("total", order.total())
                .log("placed order");
        return order;
    }

    /** An exception with a cause, to see the single-line stack trace. */
    @GetMapping("/boom")
    void boom() {
        throw new IllegalStateException(
                "payment gateway timed out", new SocketTimeoutException("read timed out"));
    }
}
