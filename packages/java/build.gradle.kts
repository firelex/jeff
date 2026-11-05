import org.gradle.api.tasks.testing.logging.TestExceptionFormat

plugins {
    `java-library`
    `maven-publish`
}

// The Maven coordinates: ai.jeffhub:jeff-sdk (the artifact id is rootProject.name, settings.gradle.kts).
group = "ai.jeffhub"
version = "1.4.0"

java {
    withSourcesJar()
    withJavadocJar()
}

repositories {
    mavenCentral()
}

dependencyLocking {
    lockAllConfigurations()
}

dependencies {
    implementation("com.fasterxml.jackson.core:jackson-core:2.22.4")
    implementation("org.yaml:snakeyaml:2.7")
    testImplementation(platform("org.junit:junit-bom:5.14.4"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    // compiled against by ProxyIsolation, the launcher session listener that pins an empty proxy environment
    testImplementation("org.junit.platform:junit-platform-launcher")
}

tasks.withType<JavaCompile>().configureEach {
    options.release.set(21)
    options.encoding = "UTF-8"
    options.compilerArgs.add("-Xlint:all,-serial")
}

// Module-path users get a stable module name instead of one derived from the jar's file name.
tasks.withType<Jar>().configureEach {
    from(rootDir.resolve("LICENSE")) { into("META-INF") }
}

tasks.jar {
    manifest {
        attributes("Automatic-Module-Name" to "ai.jeff.sdk")
    }
}

/** The repository this package lives in: packages/java -> the repository root. */
val repository: File = rootDir.resolve("../..").canonicalFile

tasks.test {
    useJUnitPlatform()
    maxHeapSize = "2g"
    systemProperty("jeff.repository", repository.absolutePath)
    testLogging {
        events("failed")
        exceptionFormat = TestExceptionFormat.FULL
    }
}

/** What the package carries from the repository, copied by syncShipped (ShippedTest fails when the copy is stale). */
val shippedItems = listOf("contract/schemas", "contract/interfaces.yaml", "contract/errors.yaml", "catalogue")
val shippedRoot: File = projectDir.resolve("src/main/resources/jeff-shipped")

tasks.register("syncShipped") {
    description = "Copies contract/schemas, contract/interfaces.yaml, contract/errors.yaml and catalogue/ from the " +
        "repository into src/main/resources/jeff-shipped, with index.txt listing every file."
    doLast {
        shippedRoot.deleteRecursively()
        val names = mutableListOf<String>()
        for (item in shippedItems) {
            val source = repository.resolve(item)
            require(source.exists()) { "$source is missing" }
            source.walkTopDown().filter { it.isFile }.forEach { file ->
                val relative = file.relativeTo(repository).invariantSeparatorsPath
                val copy = shippedRoot.resolve(relative)
                copy.parentFile.mkdirs()
                file.copyTo(copy)
                names += relative
            }
        }
        names.sort()
        shippedRoot.resolve("index.txt").writeText(names.joinToString("\n", postfix = "\n"))
        println("Copied ${names.size} files to src/main/resources/jeff-shipped")
    }
}

// The Javadoc of the public surface: every public type and member documented, no doclint warning (warnings fail it).
// `check` (and so `build`) runs it.
tasks.javadoc {
    (options as StandardJavadocDocletOptions).apply {
        encoding = "UTF-8"
        addBooleanOption("Xdoclint:all", true)
        addBooleanOption("Werror", true)
        addStringOption("Xmaxwarns", "10000")
        addStringOption("Xmaxerrs", "10000")
    }
}

tasks.check {
    dependsOn(tasks.javadoc)
}

// The release: `./gradlew publishMavenJavaPublicationToStagingRepository` writes the jar, the sources and javadoc jars,
// the POM and the Gradle module file, each with its checksums, into build/staging-deploy in the Maven layout.
// scripts/publish_sdks.sh signs those files with gpg and uploads them to Maven Central as one bundle zip; Gradle never
// sees a key or a password.
val stagingRepository: File = layout.buildDirectory.dir("staging-deploy").get().asFile

publishing {
    publications {
        create<MavenPublication>("mavenJava") {
            from(components["java"])
            artifactId = "jeff-sdk"
            versionMapping {
                allVariants { fromResolutionResult() }
            }
            pom {
                name.set("Jeff SDK for Java")
                description.set("The Jeff SDK for Java: call Jeff's decision functions, route, escalate and run decision " +
                    "graphs, against an engine on this machine or a Jeff server.")
                url.set("https://jeffhub.ai")
                licenses {
                    license {
                        name.set("MIT License")
                        url.set("https://opensource.org/license/mit")
                        distribution.set("repo")
                    }
                }
                developers {
                    developer {
                        id.set("casperaki")
                        name.set("Casperaki Multimedia Solutions Ltd.")
                        email.set("mstrasser@scissero.com")
                        organization.set("Casperaki Multimedia Solutions Ltd.")
                        organizationUrl.set("https://jeffhub.ai")
                    }
                }
                scm {
                    url.set("https://github.com/firelex/jeff/tree/main/packages/java")
                    connection.set("scm:git:https://github.com/firelex/jeff.git")
                    developerConnection.set("scm:git:ssh://git@github.com/firelex/jeff.git")
                }
            }
        }
    }
    repositories {
        maven {
            name = "staging"
            url = stagingRepository.toURI()
        }
    }
}

// Each publish starts from an empty staging folder, so the bundle holds this build's files only.
tasks.named("publishMavenJavaPublicationToStagingRepository") {
    doFirst { stagingRepository.deleteRecursively() }
}
