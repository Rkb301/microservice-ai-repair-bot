package com.example.order;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class OrderServiceAppTest {
    @Test
    void createsOrderForKnownCustomer() {
        assertTrue(OrderServiceApp.createOrder(new OrderServiceApp.Customer("42")).contains("42"));
    }
}
