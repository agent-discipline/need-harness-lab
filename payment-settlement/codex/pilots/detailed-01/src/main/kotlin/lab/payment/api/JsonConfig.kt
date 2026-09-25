package lab.payment.api

import com.fasterxml.jackson.databind.DeserializationFeature
import org.springframework.boot.autoconfigure.jackson.Jackson2ObjectMapperBuilderCustomizer
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration

@Configuration
class JsonConfig {
    @Bean
    fun integerAmountsOnly() = Jackson2ObjectMapperBuilderCustomizer {
        it.featuresToDisable(DeserializationFeature.ACCEPT_FLOAT_AS_INT)
    }
}
