package com.firdi.keycloak;

import org.keycloak.Config;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;

import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;

public class UserSyncEventListenerProviderFactory implements EventListenerProviderFactory {

    private List<String> webhookUrls;
    private String webhookSecret;

    @Override
    public EventListenerProvider create(KeycloakSession session) {
        return new UserSyncEventListenerProvider(webhookUrls, webhookSecret);
    }

    @Override
    public void init(Config.Scope config) {
        String rawUrls = System.getenv().getOrDefault(
            "USER_SYNC_WEBHOOK_URL",
            "http://host.docker.internal:8763/keycloak/user-sync"
        );

        this.webhookUrls = Arrays.stream(rawUrls.split(","))
            .map(String::trim)
            .filter(s -> !s.isEmpty())
            .collect(Collectors.toList());

        this.webhookSecret = System.getenv().getOrDefault(
            "USER_SYNC_WEBHOOK_SECRET",
            "dev-only-secret"
        );

        System.out.println("[user-sync-listener] webhookUrls=" + webhookUrls);
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
    }

    @Override
    public void close() {
    }

    @Override
    public String getId() {
        return "user-sync-listener";
    }
}