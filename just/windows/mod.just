set working-directory := "../.."

import "backend.just"
import "hil.just"

[private]
mod consumer_tasks "consumer.just"

[private]
mod example_tasks "example.just"

# Validate the standalone version-only consumer against newly staged crates.
consumer: package consumer_tasks::verify

# Explicitly refresh the tracked consumer lock for changed 0.0.0 staged packages.
consumer-refresh-lock: package-refresh-consumer-lock

# Compile and lint the user-facing Windows Central example without scanning RF.
example: example_tasks::verify
