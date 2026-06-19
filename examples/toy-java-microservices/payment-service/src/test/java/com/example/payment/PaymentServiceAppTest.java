package com.example.payment;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class PaymentServiceAppTest {
    @Test
    void chargesPositiveAmount() {
        assertTrue(PaymentServiceApp.charge(10).contains("10"));
    }
}
