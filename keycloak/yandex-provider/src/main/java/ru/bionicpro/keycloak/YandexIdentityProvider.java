package ru.bionicpro.keycloak;

import com.fasterxml.jackson.databind.JsonNode;
import org.keycloak.broker.oidc.AbstractOAuth2IdentityProvider;
import org.keycloak.broker.oidc.OAuth2IdentityProviderConfig;
import org.keycloak.broker.provider.BrokeredIdentityContext;
import org.keycloak.broker.provider.IdentityBrokerException;
import org.keycloak.http.simple.SimpleHttp;
import org.keycloak.http.simple.SimpleHttpResponse;
import org.keycloak.models.KeycloakSession;

/** Yandex OAuth 2.0 broker; uses its documented OAuth authorization header. */
public final class YandexIdentityProvider extends AbstractOAuth2IdentityProvider<OAuth2IdentityProviderConfig> {
    public YandexIdentityProvider(KeycloakSession session, OAuth2IdentityProviderConfig config) {
        super(session, config);
        config.setAuthorizationUrl("https://oauth.yandex.ru/authorize");
        config.setTokenUrl("https://oauth.yandex.ru/token");
    }

    @Override protected String getDefaultScopes() { return "login:info login:email"; }

    @Override protected BrokeredIdentityContext doGetFederatedIdentity(String accessToken) {
        try (SimpleHttpResponse response = SimpleHttp.create(session)
                .doGet("https://login.yandex.ru/info?format=json")
                .header("Authorization", "OAuth " + accessToken).asResponse()) {
            if (response.getStatus() != 200) {
                throw new IdentityBrokerException("Yandex profile request failed");
            }
            JsonNode profile = response.asJson();
            String id = profile.path("id").asText();
            if (id.isBlank()) throw new IdentityBrokerException("Missing Yandex subject");
            BrokeredIdentityContext identity = new BrokeredIdentityContext(getConfig());
            identity.setId(id);
            identity.setBrokerUserId(getConfig().getAlias() + "." + id);
            identity.setUsername("yandex." + id);
            // Authentication stores only the provider subject. Name/email are fetched
            // and persisted by bionicpro-auth after the user's explicit consent.
            return identity;
        } catch (Exception exception) {
            // Do not include provider payloads or tokens in logs or error messages.
            throw new IdentityBrokerException("Unable to authenticate with Yandex ID");
        }
    }
}
