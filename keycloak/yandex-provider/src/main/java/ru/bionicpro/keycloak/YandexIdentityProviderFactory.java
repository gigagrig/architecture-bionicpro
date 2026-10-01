package ru.bionicpro.keycloak;

import org.keycloak.broker.oidc.OAuth2IdentityProviderConfig;
import org.keycloak.broker.provider.AbstractIdentityProviderFactory;
import org.keycloak.models.IdentityProviderModel;
import org.keycloak.models.KeycloakSession;

public final class YandexIdentityProviderFactory extends AbstractIdentityProviderFactory<YandexIdentityProvider> {
    @Override public String getId() { return "yandex"; }
    @Override public String getName() { return "Яндекс ID"; }
    @Override public YandexIdentityProvider create(KeycloakSession session, IdentityProviderModel model) {
        return new YandexIdentityProvider(session, new OAuth2IdentityProviderConfig(model));
    }
    @Override public OAuth2IdentityProviderConfig createConfig() { return new OAuth2IdentityProviderConfig(); }
}
