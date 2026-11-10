package com.example.logging;

import com.example.logging.OrderController.UnknownCartException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

/**
 * Maps exceptions to RFC 9457 problem details and logs them as single JSON lines.
 *
 * <p>Extending {@link ResponseEntityExceptionHandler} keeps Spring MVC's own exceptions on their
 * status codes (400 for a missing parameter, 404 for an unknown path, 405, 406 and so on). Without
 * it the {@code Exception} handler below would answer every client mistake with a 500 and an {@code
 * ERROR} line carrying a stack trace.
 */
@RestControllerAdvice
class ApiExceptionHandler extends ResponseEntityExceptionHandler {

    // Not "logger": the superclass has a protected field of that name.
    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);

    @ExceptionHandler(UnknownCartException.class)
    ProblemDetail handleUnknownCart(UnknownCartException ex) {
        log.atWarn().addKeyValue("cart_id", ex.cartId()).log("unknown cart");
        return ProblemDetail.forStatusAndDetail(HttpStatus.NOT_FOUND, ex.getMessage());
    }

    /**
     * Last-resort handler: the stack trace lands in {@code message}, one line, trace-correlated.
     */
    @ExceptionHandler(Exception.class)
    ProblemDetail handleUnexpected(Exception ex) {
        log.error("request failed", ex);
        return ProblemDetail.forStatus(HttpStatus.INTERNAL_SERVER_ERROR);
    }
}
