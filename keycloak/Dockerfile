FROM maven:3.9.9-eclipse-temurin-21 AS provider
WORKDIR /build
COPY yandex-provider/pom.xml .
COPY yandex-provider/src ./src
RUN mvn -B -q package

FROM quay.io/keycloak/keycloak:26.8.0
COPY --from=provider /build/target/yandex-provider-1.0.jar /opt/keycloak/providers/
