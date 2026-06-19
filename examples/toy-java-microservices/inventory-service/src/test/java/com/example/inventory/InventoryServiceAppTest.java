package com.example.inventory;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class InventoryServiceAppTest {
    @Test
    void reservesPositiveQuantity() {
        assertTrue(InventoryServiceApp.reserve("SKU-1", 1).contains("true"));
    }
}
