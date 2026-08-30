package dev.blehub.spike

object SinkExceptionProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val builder = BleHubBuilder(BleEventSink {
            error("undeclared sink failure")
        })
        val ingress = builder.backendEventSink()
        val backend = object : BleBackend {
            override fun startScan(command: StartScanCommand) = Unit

            override fun dispose() = Unit
        }
        val hub = builder.finish(backend)

        try {
            try {
                val status = ingress.push(BleEvent(1u, null, byteArrayOf(1)))
                check(status == EventIngressStatus.DOWNSTREAM_FAILED)
                println("SAFE:unchecked sink exception mapped to DOWNSTREAM_FAILED")
            } catch (error: Throwable) {
                println("SURVIVED:exception=${error.javaClass.name}:${error.message}")
            }
        } finally {
            hub.dispose()
            ingress.close()
            hub.close()
            builder.close()
        }
    }
}
