FROM eclipse-temurin:21-jdk AS build
WORKDIR /workspace
COPY . .
RUN chmod +x gradlew && ./gradlew --no-daemon clean test bootJar

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /workspace/build/libs/app.jar app.jar
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["java", "-Xms256m", "-Xmx768m", "-XX:ActiveProcessorCount=2", "-jar", "/app/app.jar"]
