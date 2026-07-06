package com.firdi.keycloak;

import org.keycloak.events.Event;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventType;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.events.admin.OperationType;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;

public class UserSyncEventListenerProvider implements EventListenerProvider {

    private final List<String> webhookUrls;
    private final String webhookSecret;

    public UserSyncEventListenerProvider(List<String> webhookUrls, String webhookSecret) {
        this.webhookUrls = webhookUrls;
        this.webhookSecret = webhookSecret;
    }

    @Override
    public void onEvent(Event event) {
        EventType type = event.getType();

        if (
            type == EventType.REGISTER ||
            type == EventType.UPDATE_PROFILE
        ) {
            String userId = event.getUserId();
            if (userId != null && !userId.isBlank()) {
                sendWebhook(userId, type.name(), "user_event");
            }
        }
    }

    @Override
    public void onEvent(AdminEvent adminEvent, boolean includeRepresentation) {
        OperationType operationType = adminEvent.getOperationType();
        String resourcePath = adminEvent.getResourcePath();

        if (resourcePath == null) {
            return;
        }

        // 常見格式：
        // users/{user_id}
        // users/{user_id}/groups/{group_id}
        // users/{user_id}/role-mappings/...
        if (!resourcePath.startsWith("users/")) {
            return;
        }

        if (
            operationType == OperationType.CREATE ||
            operationType == OperationType.UPDATE ||
            operationType == OperationType.DELETE ||
            operationType == OperationType.ACTION
        ) {
            String userId = extractUserId(resourcePath);
            if (userId != null && !userId.isBlank()) {
                sendWebhook(userId, operationType.name(), "admin_event");
            }
        }
    }

    private String extractUserId(String resourcePath) {
        String[] parts = resourcePath.split("/");
        if (parts.length >= 2 && "users".equals(parts[0])) {
            return parts[1];
        }
        return null;
    }

    private void sendWebhook(String userId, String eventType, String source) {
        String json = """
            {
              "user_id": "%s",
              "event_type": "%s",
              "source": "%s"
            }
            """.formatted(userId, eventType, source);

        for (String webhookUrl : webhookUrls) {
            try {
                URL url = new URL(webhookUrl);
                HttpURLConnection conn = (HttpURLConnection) url.openConnection();

                conn.setRequestMethod("POST");
                conn.setConnectTimeout(3000);
                conn.setReadTimeout(5000);
                conn.setDoOutput(true);

                conn.setRequestProperty("Content-Type", "application/json");
                conn.setRequestProperty("X-Webhook-Secret", webhookSecret);

                try (OutputStream os = conn.getOutputStream()) {
                    os.write(json.getBytes(StandardCharsets.UTF_8));
                }

                int status = conn.getResponseCode();

                if (status < 200 || status >= 300) {
                    System.err.println("[user-sync-listener] Webhook failed. url=" + webhookUrl + " status=" + status);
                }

                conn.disconnect();

            } catch (Exception e) {
                System.err.println("[user-sync-listener] Failed to send webhook. url=" + webhookUrl + " error=" + e.getMessage());
            }
        }
    }

    @Override
    public void close() {
    }
}