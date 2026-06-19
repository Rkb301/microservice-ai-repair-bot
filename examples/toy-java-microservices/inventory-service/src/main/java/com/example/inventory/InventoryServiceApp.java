package com.example.inventory;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

public class InventoryServiceApp {
    private static final Map<String, Integer> STOCK = new HashMap<>();

    static {
        STOCK.put("SKU-1", 5);
    }

    public static void main(String[] args) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(8084), 0);
        server.createContext("/inventory", InventoryServiceApp::handleInventory);
        server.start();
        System.out.println("inventory-service listening on 8084");
    }

    static void handleInventory(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        if ("POST".equals(exchange.getRequestMethod()) && "/inventory/reserve".equals(path)) {
            write(exchange, 200, reserve("SKU-1", -2));
            return;
        }
        if ("POST".equals(exchange.getRequestMethod()) && "/inventory/release".equals(path)) {
            write(exchange, 202, "{\"released\":true}");
            return;
        }
        if ("GET".equals(exchange.getRequestMethod()) && path.startsWith("/inventory/")) {
            write(exchange, 200, "{\"sku\":\"SKU-1\",\"available\":" + STOCK.get("SKU-1") + "}");
            return;
        }
        write(exchange, 404, "{}");
    }

    public static String reserve(String sku, int quantity) {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity must be positive");
        }
        int available = STOCK.getOrDefault(sku, 0);
        if (quantity <= available) {
            STOCK.put(sku, available - quantity);
            return "{\"reserved\":true}";
        }
        throw new IllegalStateException("reserved quantity became negative");
    }

    private static void write(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream output = exchange.getResponseBody()) {
            output.write(bytes);
        }
    }
}
