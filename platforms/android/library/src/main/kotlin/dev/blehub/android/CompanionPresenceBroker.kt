package dev.blehub.android

/**
 * Process-scoped bridge from the platform-bound companion-device service to the live backend.
 *
 * The system instantiates [BleHubCompanionDeviceService] in this process, and only while an
 * association has asked for presence. That service cannot see the backend's owner-scoped peer
 * cache, and the backend cannot see the service's association record, so this registry passes the
 * platform address (and association label) between them and nothing else.
 *
 * One process has at most one backend, enforced by the backend lease, so a single sink is the whole
 * binding. A last bind wins after the previous backend is disposed; the previous backend unbinds
 * itself on disposal, so a stale sink can never receive a later backend's events.
 */
internal object CompanionPresenceBroker {
    /**
     * Receives one presence transition.
     *
     * Runs on the caller's thread (the service's main thread), never on the backend handler, so an
     * implementation is responsible for hopping onto its own confinement.
     */
    fun interface Sink {
        fun onPresence(address: String, localName: String?, present: Boolean)
    }

    private val lock = Any()
    private var sink: Sink? = null

    fun bind(sink: Sink) {
        synchronized(lock) { this.sink = sink }
    }

    fun unbind(sink: Sink) {
        synchronized(lock) {
            if (this.sink === sink) this.sink = null
        }
    }

    fun publish(address: String, localName: String?, present: Boolean) {
        val current = synchronized(lock) { sink } ?: return
        current.onPresence(address, localName, present)
    }
}
