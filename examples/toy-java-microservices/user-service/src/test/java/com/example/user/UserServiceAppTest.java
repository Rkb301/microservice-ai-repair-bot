package com.example.user;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class UserServiceAppTest {
    @Test
    void returnsKnownProfile() {
        assertTrue(UserServiceApp.getProfile("42").contains("FOUNDER"));
    }
}
