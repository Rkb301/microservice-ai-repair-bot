# Toy Java Microservices

Four safe, public-GitHub-friendly Java 17 toy services are included:

- `user-service` on port `8081`
- `order-service` on port `8082`
- `payment-service` on port `8083`
- `inventory-service` on port `8084`

Each service uses in-memory data only, includes no real credentials, and exposes three simple HTTP endpoints. They are deliberately small so the Python repair bot can inspect and patch them during the prototype phase.

Run tests for one service:

```bash
cd user-service
mvn test
```

Run a service:

```bash
mvn -q -DskipTests package
java -cp target/classes com.example.user.UserServiceApp
```

The traffic generator can create synthetic logs without starting the services, or it can send HTTP traffic with `--send-http` after the services are running.
