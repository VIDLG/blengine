package com.blehubblenghost

import android.os.SystemClock
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.StaleObjectException
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.UiObject2
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * One UI scene driven in-process by UiAutomator, instead of over WebDriver.
 *
 * This is the Android executor of the *same* host-owned plan vocabulary the iOS
 * `BlengSceneDriverUITests` target runs (`tools/react_native/scene_plans.py`): `wait`, `waitAny`,
 * `still`, `tap`, `tapWhen`, `tapUntil`, `read`, `setText`, `activate`, `background`,
 * `answerAlert`. There is no Appium server, no UiAutomator2 server, no HTTP hop and no
 * element-id round trip: the plan is a file the host wrote, the receipt is a file the host
 * reads back.
 *
 * ```json
 * {"launch": false, "steps": [
 *   {"op": "tap",  "id": "start-session"},
 *   {"op": "wait", "id": "run-status", "status": {"state": "started"}},
 *   {"op": "read", "id": "run-status", "as": "started"},
 *   {"op": "tap",  "id": "close-session"},
 *   {"op": "wait", "id": "run-status", "status": {"state": "closed"}}
 * ]}
 * ```
 *
 * **There is no scroll, reveal or coordinate step, and that is a property of the design.** A
 * scene only reads or acts on the App's pinned machine surface: the `run-status` scalar projection
 * plus the elements deliberately rendered outside every scroll container. React Native exposes
 * `testID` as the view's accessibility `viewIdResourceName`, so `By.res("<testID>")` is the same
 * identifier contract the iOS target uses through `accessibilityIdentifier`.
 *
 * Conditions are a conjunction of assertions on ONE element: `exists`, `label`, `labelContains`,
 * `enabled`, `status` (decode `key=value;...` from the App's own projection) and `value` (a text
 * field's current text). `still` is the one temporal step over the App's own surface; `background`
 * is the platform's, leaving the App and holding it away for its declared `for` window; `waitAny`
 * is a bounded disjunction; `tapUntil` is a bounded pull loop that may also declare `advance` —
 * a status counter it must see grow, so a step whose expected value already equals the previous
 * command's value still taps instead of settling on it. `activate` foregrounds the App this
 * run already launched.
 *
 * The step vocabulary is protocol, not result: the receipt reports what each step observed, and
 * the host decides what a scene may claim. Stage markers are logcat diagnostics only; the
 * host learns that the scene ended from the `done` file this driver publishes last, because
 * a logcat line may be a replayed one from an earlier scene and a file this run created
 * cannot be.
 */
class BlengSceneDriverInstrumentedTest {
    private companion object {
        const val TAG = "BLEHUB_RN_SCENE"
        const val POLL_MS = 100L
        const val DEFAULT_TIMEOUT_MS = 30_000L

        // How long the App gets to stop being frontmost after Home. iOS/Android both take a
        // moment to hand focus to the launcher; 15 s only ever waits out a slow transition.
        const val BACKGROUND_GRACE_MS = 15_000L

        // Holding the App away is not a transition to race: one sample a second still catches a
        // return, and every sample is a cross-process query.
        const val DWELL_POLL_MS = 1_000L
        const val MAX_DWELL_SECONDS = 60.0
        const val MAX_TAPS = 64
        const val MAX_BYTES = 1 shl 20

        const val DEFAULT_TAP_TIMEOUT_MS = 30_000L

        fun marker(name: String, detail: String = "") {
            Log.i(TAG, if (detail.isEmpty()) name else "$name:$detail")
        }
    }

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val device = UiDevice.getInstance(instrumentation)
    private val appPackage: String by lazy { instrumentation.targetContext.packageName }

    @Test
    fun scenePlan() {
        assumeTrue("No scene plan: this driver is host-driven only", argument("plan") != null)
        val planPath = requireArgument("plan")
        val receiptPath = requireArgument("receipt")
        val hierarchyPath = argument("hierarchy")
        val donePath = requireArgument("done")
        val budgetMs = argument("budgetMs")?.toLongOrNull()
        val deadline = if (budgetMs == null) {
            Long.MAX_VALUE
        } else {
            SystemClock.uptimeMillis() + budgetMs
        }

        val plan = JSONObject(readShell(planPath))
        val steps = plan.optJSONArray("steps") ?: JSONArray()
        marker("scene", plan.optString("name", "unnamed"))

        val records = JSONArray()
        val values = JSONObject()
        var failure: String? = null
        // `launch` starts a new App process, for the standalone entry. A host-owned scene
        // installs, launches and pins one pid itself and passes `"launch": false`; its plan then
        // foregrounds the App with the `activate` step instead.
        if (plan.optBoolean("launch", true)) {
            try {
                launchApp()
            } catch (error: Throwable) {
                failure = "launch failed: ${describe(error)}"
            }
        }
        if (failure == null) {
            for (index in 0 until steps.length()) {
                val step = steps.getJSONObject(index)
                val observed = JSONObject()
                val started = SystemClock.uptimeMillis()
                try {
                    requireBudget(deadline, "step $index")
                    perform(step, deadline, observed, values)
                } catch (error: Throwable) {
                    records.put(
                        JSONObject()
                            .put("index", index)
                            .put("step", step)
                            .put("seconds", secondsSince(started))
                            .put("error", describe(error))
                            .put("observed", observed),
                    )
                    failure = "scene step $index $step failed: ${describe(error)}"
                    break
                }
                records.put(
                    JSONObject()
                        .put("index", index)
                        .put("step", step)
                        .put("seconds", secondsSince(started))
                        .put("observed", observed),
                )
                marker("step", "$index:${step.optString("op")}")
            }
        }

        val payload = JSONObject()
            .put("scope", "android-scene-driver")
            .put("status", if (failure == null) "passed" else "failed")
            .put("primary_error", failure ?: "")
            .put("steps", records)
            .put("values", values)
        publish(receiptPath, payload.toString(), "receipt")
        // The hierarchy is evidence, not only a failure aid: it is the only record of what the
        // screen showed after the last step, and the test process's stdout never leaves it.
        if (hierarchyPath != null) {
            dumpHierarchy(hierarchyPath)
        }
        // Last, always (a failed step publishes it too): its presence is what tells the host
        // that nothing more will be written and it may stop this process.
        publish(donePath, if (failure == null) "passed\n" else "failed\n", "done")
        marker("result", if (failure == null) "passed" else "failed")
        if (failure != null) {
            fail(failure)
        }
    }

    // -- steps ----------------------------------------------------------------

    private fun perform(
        step: JSONObject,
        deadline: Long,
        observed: JSONObject,
        values: JSONObject,
    ) {
        val timeout = step.optLong("timeout", DEFAULT_TIMEOUT_MS / 1000) * 1000
        when (val op = step.getString("op")) {
            "tap" -> tap(requireID(step), timeout, observed)

            "tapWhen" -> {
                val whenCondition = step.getJSONObject("when")
                awaitCondition(
                    whenCondition,
                    requireID(whenCondition),
                    timeout,
                    observed,
                )
                tap(requireID(step), timeout, observed)
            }

            "tapUntil" -> tapUntil(step, timeout, observed)
            "wait" -> awaitCondition(step, requireID(step), timeout, observed)
            "waitAny" -> waitAny(step, timeout, observed)
            "still" -> still(step, timeout, deadline, observed)
            "activate" -> activate(step, observed)
            "background" -> background(step, observed)
            "handoff" -> handoff(step, timeout, observed)
            "setText" -> setText(step, timeout, observed)
            "answerAlert" -> answerAlert(step, timeout, observed)
            "read" -> {
                val id = requireID(step)
                val name = step.optString("as")
                require(name.isNotEmpty()) { "read requires `as`" }
                values.put(name, awaitLabel(step, id, timeout, observed))
            }

            else -> throw PlanError("unknown scene step op $op")
        }
    }

    /**
     * Tap a control once it is actionable.
     *
     * A React control renders `disabled` while its own submit is in flight, so presence alone
     * is not actionable. Waiting for `isEnabled` is a property of the pinned surface, not a
     * gesture, and the node is re-resolved each poll because a re-render replaces the view.
     */
    private fun tap(id: String, timeout: Long, observed: JSONObject) {
        val deadline = SystemClock.uptimeMillis() + timeout
        while (true) {
            val node = awaitNode(id, deadline, observed)
            try {
                if (node.isEnabled) {
                    node.click()
                    return
                }
            } catch (error: StaleObjectException) {
                // Replaced between resolve and read; the next poll sees the new view.
            }
            observed.put(id, "disabled")
            requireBudget(deadline, "$id enabled")
            Thread.sleep(POLL_MS)
        }
    }

    private fun tapUntil(step: JSONObject, timeout: Long, observed: JSONObject) {
        val id = requireID(step)
        val until = step.getJSONObject("until")
        val untilID = requireID(until)
        val maxTaps = step.optInt("maxTaps", 8)
        if (maxTaps !in 1..MAX_TAPS) {
            throw PlanError("maxTaps must be 1..$MAX_TAPS")
        }
        // `advance`: the step is only satisfied once the named status counter is strictly
        // greater than it was when the step began. It exists because a control whose effect
        // can equal the value already on screen (a re-read that reports the same bytes) would
        // otherwise be satisfied by the *previous* command's value without being tapped at all.
        // The baseline is fixed for the whole step: it means "one newer completion than when
        // this step started", never "newer than the last poll".
        val advance = step.optString("advance", "")
        val floor = if (advance.isEmpty()) 0L else statusCount(untilID, advance, observed)
        var settled = floor
        val deadline = SystemClock.uptimeMillis() + timeout
        var attempt = 0
        while (true) {
            if (satisfied(until, untilID, observed, advance, floor)) {
                observed.put("taps", attempt.toString())
                return
            }
            if (attempt >= maxTaps) {
                throw PlanError("$untilID still not satisfied after $maxTaps taps of $id")
            }
            tap(id, remaining(deadline), observed)
            attempt += 1
            if (advance.isNotEmpty()) {
                // Wait for *this* tap's own operation to settle before deciding to tap again.
                // Otherwise a fast poll loop records the same command several times and the
                // App's bounded ledger loses the scene's first actions.
                settled = awaitStatusAbove(untilID, advance, settled, deadline, observed)
            }
        }
    }

    /**
     * Polls a status counter until it is strictly greater than `above`.
     *
     * A counter that never moves is a step whose operation never settled, so it fails here
     * with a named budget instead of tapping again in the hope of a different answer.
     */
    private fun awaitStatusAbove(
        id: String,
        field: String,
        above: Long,
        deadline: Long,
        observed: JSONObject,
    ): Long {
        while (true) {
            val count = statusCount(id, field, observed)
            if (count > above) return count
            requireBudget(deadline, "$id $field above $above")
            Thread.sleep(POLL_MS)
        }
    }

    /** One numeric status field, or 0 when it is absent/unparsable/unrendered. */
    private fun statusCount(id: String, field: String, observed: JSONObject? = null): Long =
        try {
            val node = find(id)
            val label = labelOf(node)
            observed?.put(id, if (node == null) "absent" else "exists label=$label")
            statusCountOf(label, field)
        } catch (error: StaleObjectException) {
            observed?.put(id, "stale")
            0L
        }

    private fun waitAny(step: JSONObject, timeout: Long, observed: JSONObject) {
        val any = step.optJSONArray("any")
        if (any == null || any.length() !in 2..8) {
            throw PlanError("waitAny requires 2...8 conditions")
        }
        val conditions = (0 until any.length()).map { any.getJSONObject(it) }
        val ids = conditions.map { requireID(it) }
        val deadline = SystemClock.uptimeMillis() + timeout
        while (true) {
            for (index in conditions.indices) {
                if (satisfied(conditions[index], ids[index], observed)) {
                    observed.put("waitAny", index.toString())
                    return
                }
            }
            requireBudget(deadline, "waitAny ${ids.joinToString()}")
            Thread.sleep(POLL_MS)
        }
    }

    private fun still(step: JSONObject, timeout: Long, deadline: Long, observed: JSONObject) {
        val id = requireID(step)
        val window = step.optDouble("for", 0.0)
        if (window <= 0.0 || window > 30.0) {
            throw PlanError("still requires `for` in (0, 30] seconds")
        }
        val sampledUntil = minOf(SystemClock.uptimeMillis() + timeout, deadline)
        val quietUntil = SystemClock.uptimeMillis() + (window * 1000).toLong()
        while (true) {
            if (!satisfied(step, id, observed)) {
                throw PlanError(
                    "$id did not hold for ${window}s: ${describe(step)} " +
                            "(saw ${observed.optString(id, "absent")})",
                )
            }
            if (SystemClock.uptimeMillis() >= quietUntil) {
                observed.put("still", "${window}s")
                return
            }
            requireBudget(sampledUntil, "still $id")
            Thread.sleep(POLL_MS)
        }
    }

    /**
     * Foreground the App this run already launched; never a relaunch of a new process.
     *
     * Home first, then an explicit `am start -W` (shell identity, so background-start
     * restrictions do not apply) on the App's `singleTask` activity: the existing task comes
     * forward instead of a second instance. `am start -W` returning is not the same as the App
     * being focused, so the step waits for the focused window's own package.
     */
    private fun activate(step: JSONObject, observed: JSONObject) {
        // One cheap question first: a focused window of this package *is* the state this step
        // exists to establish, and the check is the same fact the slow path waits for. In a
        // one-process scene every later phase finds it already true, so re-establishing it
        // would pay a Home press, a sleep and an `am start` round trip for nothing (measured
        // at 1.3 s of 47 calls). A foreign window — a system panel, the launcher — still
        // takes the full path, which is what dismisses it.
        if (device.currentPackageName == appPackage) {
            observed.put("activate", "already-foreground")
            return
        }
        val component = requireArgument("component")
        device.pressHome()
        Thread.sleep(300)
        device.executeShellCommand("am start -W -n $component")
        val deadline = SystemClock.uptimeMillis() +
                step.optLong("timeout", DEFAULT_TIMEOUT_MS / 1000) * 1000
        while (true) {
            if (device.currentPackageName == appPackage) {
                observed.put("activate", "foreground")
                return
            }
            requireBudget(deadline, "activate $appPackage")
            Thread.sleep(POLL_MS)
        }
    }

    /**
     * Put the App in the background and hold it there for the plan's declared window.
     *
     * The inverse of `activate`: there the focused window proves this package is frontmost,
     * here it proves it is not. The step fails rather than continuing in the foreground,
     * because everything the scene asserts afterwards is about a world where the App was away
     * — a host action taken in front of the App would make those assertions describe a
     * different scene.
     *
     * `for` is a window to hold, not a timeout to wait out: an App that was away for an instant
     * proves nothing, so the same fact is sampled for the declared window and the step fails if
     * the App comes back early. The window is the plan's, never this driver's, and the observed
     * value records how long the App was actually away.
     */
    private fun background(step: JSONObject, observed: JSONObject) {
        val window = step.optDouble("for", 0.0)
        if (window <= 0.0 || window > MAX_DWELL_SECONDS) {
            throw PlanError("background requires `for` in (0, $MAX_DWELL_SECONDS] seconds")
        }
        device.pressHome()
        val graceUntil = SystemClock.uptimeMillis() + BACKGROUND_GRACE_MS
        while (device.currentPackageName == appPackage) {
            if (SystemClock.uptimeMillis() >= graceUntil) {
                observed.put("background", "still-foreground")
                throw PlanError(
                    "the App is still foreground ${BACKGROUND_GRACE_MS / 1000}s after backgrounding"
                )
            }
            Thread.sleep(POLL_MS)
        }
        val awaySince = SystemClock.uptimeMillis()
        val holdUntil = awaySince + (window * 1000).toLong()
        while (true) {
            if (device.currentPackageName == appPackage) {
                observed.put(
                    "background",
                    "resumed after ${seconds(awaySince)}s of ${window}s",
                )
                throw PlanError("the App returned to the foreground inside the ${window}s window")
            }
            if (SystemClock.uptimeMillis() >= holdUntil) {
                observed.put("background", "not-foreground for=${seconds(awaySince)}s")
                return
            }
            Thread.sleep(DWELL_POLL_MS)
        }
    }

    private fun seconds(since: Long): String =
        String.format(java.util.Locale.ROOT, "%.1f", (SystemClock.uptimeMillis() - since) / 1000.0)

    /**
     * Suspend while the host performs its own action in this same App process.
     *
     * This exists because an instrumented test runs inside the App's own process: a host
     * action between two separate runs could never act on the BLE state this process holds.
     * The step announces the action, then waits for the host's acknowledgement file, so the
     * ordering is exact in both directions and the host is still the one guarding and
     * performing the fixture/radio action.
     */
    private fun handoff(step: JSONObject, timeout: Long, observed: JSONObject) {
        val action = step.optString("action")
        if (!action.matches(Regex("[A-Za-z0-9._-]+"))) {
            throw PlanError("handoff requires a safe `action`")
        }
        // A file handshake, not a log stream: the host must answer even if its logcat
        // capture has died, so the request is a file the host can see and the answer is a
        // file the driver waits for. Both are deleted for every occurrence, because a scene
        // repeats one action once per cycle.
        val request = File(filesDir(), "handoff-request-$action")
        val ack = File(filesDir(), "handoff-ack-$action")
        ack.delete()
        request.writeText(action, Charsets.UTF_8)
        marker("handoff", action)
        val deadline = SystemClock.uptimeMillis() + timeout
        while (!ack.exists()) {
            requireBudget(deadline, "handoff $action")
            Thread.sleep(POLL_MS)
        }
        ack.delete()
        request.delete()
        observed.put("handoff", action)
    }

    /**
     * Replace one text field's value and prove the App kept it.
     *
     * A controlled React input can drop characters while its own clear/re-render runs, so the
     * field is deleted until it *really* reads empty and the declared text is verified exactly.
     * The node is re-resolved on every poll: a React re-render replaces the view, and a stale
     * `UiObject2` would read a cached value.
     */
    private fun setText(step: JSONObject, timeout: Long, observed: JSONObject) {
        val id = requireID(step)
        val text = step.getString("text")
        val deadline = SystemClock.uptimeMillis() + timeout
        // Clear through the accessibility action, not by tapping first: a tap on an
        // `EditText` raises the soft keyboard, which reflows the App's own ScrollView and
        // can move a pinned field out of the Android accessibility tree mid-step.
        var previous = "\u0000"
        while (true) {
            val node = awaitNode(id, deadline, observed)
            val existing = node.text ?: ""
            observed.put(id, "value=$existing")
            if (existing.isEmpty()) break
            if (existing == previous) {
                writeText(id, "", deadline, observed)
            }
            previous = existing
            Thread.sleep(POLL_MS)
        }
        if (text.isNotEmpty()) {
            writeText(id, text, deadline, observed)
        }
        while (true) {
            val shown = find(id)?.text ?: ""
            observed.put(id, "value=$shown")
            if (shown == text) return
            requireBudget(deadline, "$id verify")
            Thread.sleep(POLL_MS)
        }
    }

    /**
     * Write one field's text.
     *
     * `UiObject2.setText` dispatches `ACTION_SET_TEXT`, so the field is not tapped and no
     * keyboard is raised. Only an editor that refuses that action is focused explicitly.
     */
    private fun writeText(id: String, value: String, deadline: Long, observed: JSONObject) {
        val node = awaitNode(id, deadline, observed)
        try {
            node.setText(value)
        } catch (error: Throwable) {
            observed.put("$id.focus", describe(error))
            tap(id, timeout = DEFAULT_TAP_TIMEOUT_MS, observed = observed)
            awaitNode(id, deadline, observed).setText(value)
        }
    }

    /** Resolve an identifier that a re-render may have momentarily removed. */
    private fun awaitNode(id: String, deadline: Long, observed: JSONObject): UiObject2 {
        while (true) {
            find(id)?.let { return it }
            observed.put(id, "absent")
            requireBudget(deadline, "$id presence")
            Thread.sleep(POLL_MS)
        }
    }

    /**
     * Answer *identified* system alerts, or refuse.
     *
     * A freshly installed App can assert a platform permission prompt that sits above the App
     * and swallows taps. Such a prompt belongs to another package, so this is the one step that
     * looks outside the App.
     *
     * The *button* is the discriminating signal, not the title: the notification shade and
     * status bar are another package too, and their labels contain words like "Bluetooth" in
     * ordinary use. So an alert is identified by (a) a clickable node whose text or description
     * is exactly one of the declared `buttons`, (b) in a foreign package, (c) where a node of
     * that *same* package contains one of the declared `match` strings. Anything else is left
     * alone and recorded; an unidentified alert is fatal only when the plan said `required`.
     */
    private fun answerAlert(step: JSONObject, timeout: Long, observed: JSONObject) {
        val raw = step.optJSONArray("rules")
        if (raw == null || raw.length() == 0) {
            throw PlanError("answerAlert requires a non-empty `rules` list")
        }
        val rules = (0 until raw.length()).map { index ->
            val rule = raw.getJSONObject(index)
            val match = rule.optJSONArray("match")
            val buttons = rule.optJSONArray("buttons")
            if (match == null || match.length() == 0 || buttons == null || buttons.length() == 0) {
                throw PlanError("an alert rule requires non-empty `match` and `buttons` lists")
            }
            (0 until match.length()).map { match.getString(it) } to
                    (0 until buttons.length()).map { buttons.getString(it) }
        }
        val required = step.optBoolean("required", false)
        // The shared plan vocabulary states the same window on both platforms: a prompt this
        // step does not require is watched for `grace`, and 0 means one look (the iOS executor
        // reads the field the same way). `timeout` remains the bound of a `required` prompt.
        val graceMs = (step.optDouble("grace", 0.0) * 1000).toLong()
        if (graceMs < 0) {
            throw PlanError("answerAlert `grace` must be a finite non-negative number")
        }
        // One cheap question before the per-rule scans: if our own window is frontmost,
        // nothing foreign can be covering it. The scans below cost a query per declared
        // button and description, which is why an optional step that spent its whole timeout
        // looking for a prompt nobody raised was the most expensive step of every scene.
        if (!required && device.currentPackageName == appPackage) {
            observed.put("alert", "absent")
            return
        }
        val deadline = SystemClock.uptimeMillis() +
                (if (required) timeout else minOf(timeout, graceMs))
        var answered = 0
        while (true) {
            val alert = findAlert(rules)
            if (alert == null) {
                if (answered > 0) return
                if (SystemClock.uptimeMillis() >= deadline) break
                Thread.sleep(POLL_MS)
                continue
            }
            val (button, label, title) = alert
            button.click()
            answered += 1
            observed.put("alert", "answered[$answered] label=$label button=$title")
            if (answered >= 3) return
            Thread.sleep(200)
        }
        // Only a step that answered nothing may say nothing was there: an answered prompt is
        // a fact the receipt keeps, even when its window closed before the loop noticed.
        if (answered == 0) observed.put("alert", "absent")
        if (required) {
            throw PlanError("no system alert appeared within ${timeout / 1000}s")
        }
    }

    /** A declared button in a foreign package whose window also shows a declared title. */
    private fun findAlert(
        rules: List<Pair<List<String>, List<String>>>,
    ): Triple<UiObject2, String, String>? {
        for ((match, buttons) in rules) {
            for (buttonLabel in buttons) {
                val button = device.findObjects(By.text(buttonLabel))
                    .firstOrNull { it.isClickable && it.applicationPackage != appPackage }
                    ?: device.findObjects(By.desc(buttonLabel))
                        .firstOrNull { it.isClickable && it.applicationPackage != appPackage }
                    ?: continue
                val owner = button.applicationPackage ?: continue
                val title = match.firstNotNullOfOrNull { needle ->
                    device.findObjects(By.textContains(needle))
                        .firstOrNull { it.applicationPackage == owner }
                        ?: device.findObjects(By.descContains(needle))
                            .firstOrNull { it.applicationPackage == owner }
                } ?: continue
                return Triple(button, buttonLabel, labelOf(title))
            }
        }
        return null
    }

    // -- conditions -----------------------------------------------------------

    /** Wait until [condition] holds on [id]; `exists: false` is a legitimate answer. */
    private fun awaitCondition(
        condition: JSONObject,
        id: String,
        timeout: Long,
        observed: JSONObject,
    ) {
        if (id.isEmpty()) throw PlanError("condition requires `id`")
        val deadline = SystemClock.uptimeMillis() + timeout
        while (true) {
            if (satisfied(condition, id, observed)) return
            if (SystemClock.uptimeMillis() >= deadline) {
                throw PlanError(
                    "$id did not reach ${describe(condition)} within ${timeout / 1000}s " +
                            "(saw ${observed.optString(id, "absent")})",
                )
            }
            Thread.sleep(POLL_MS)
        }
    }

    /** Wait for [id] and return the label the App currently renders for it. */
    private fun awaitLabel(
        condition: JSONObject,
        id: String,
        timeout: Long,
        observed: JSONObject,
    ): String {
        if (id.isEmpty()) throw PlanError("condition requires `id`")
        val deadline = SystemClock.uptimeMillis() + timeout
        while (true) {
            try {
                val node = find(id)
                val label = labelOf(node)
                observed.put(id, if (node == null) "absent" else "exists label=$label")
                if (matches(condition, node, label)) return label
            } catch (error: StaleObjectException) {
                observed.put(id, "stale")
            }
            if (SystemClock.uptimeMillis() >= deadline) {
                throw PlanError(
                    "$id did not reach ${describe(condition)} within ${timeout / 1000}s " +
                            "(saw ${observed.optString(id, "absent")})",
                )
            }
            Thread.sleep(POLL_MS)
        }
    }

    /**
     * One observation of one element.
     *
     * The answer is a plain boolean: `exists: false` is a legitimate condition, so no node
     * object can stand in for "matched". A node that its own React re-render replaced
     * between resolving and reading it is simply re-observed on the next poll.
     */
    private fun satisfied(condition: JSONObject, id: String, observed: JSONObject): Boolean =
        satisfied(condition, id, observed, advance = "", floor = 0L)

    /**
     * One observation of one element, optionally requiring a counter to have advanced.
     *
     * `advance` names a numeric status field that must be strictly greater than `floor` on the
     * same element. Both conditions are read from one state: two separate reads could each be
     * true at different instants and compose into a claim that never held.
     */
    private fun satisfied(
        condition: JSONObject,
        id: String,
        observed: JSONObject,
        advance: String,
        floor: Long,
    ): Boolean =
        try {
            val node = find(id)
            val label = labelOf(node)
            observed.put(id, if (node == null) "absent" else "exists label=$label")
            matches(condition, node, label) &&
                    (advance.isEmpty() || statusCountOf(label, advance) > floor)
        } catch (error: StaleObjectException) {
            observed.put(id, "stale")
            false
        }

    private fun statusCountOf(label: String, field: String): Long {
        val status = decodeStatus(label) ?: return 0L
        return status[field]?.toLongOrNull() ?: 0L
    }

    private fun matches(condition: JSONObject, node: UiObject2?, label: String): Boolean {
        val assertsValue = condition.has("label") || condition.has("labelContains") ||
                condition.has("status") || condition.has("enabled") || condition.has("value")
        val requiresPresent: Boolean
        if (condition.has("exists")) {
            val expected = condition.getBoolean("exists")
            if (!expected && assertsValue) {
                throw PlanError("an absent condition cannot also assert a value")
            }
            requiresPresent = expected
        } else {
            requiresPresent = true
        }
        if (!requiresPresent) return node == null
        if (node == null) return false
        if (condition.has("label") && label != condition.getString("label")) return false
        if (condition.has("labelContains") &&
            !label.contains(condition.getString("labelContains"), ignoreCase = true)
        ) {
            return false
        }
        if (condition.has("enabled") && node.isEnabled != condition.getBoolean("enabled")) {
            return false
        }
        if (condition.has("value") && (node.text ?: "") != condition.getString("value")) {
            return false
        }
        if (condition.has("status")) {
            val expected = condition.getJSONObject("status")
            val observedStatus = decodeStatus(label) ?: return false
            for (key in expected.keys()) {
                if (observedStatus[key] != expected.getString(key)) return false
            }
        }
        return true
    }

    /**
     * Decode `key=value;key=value` (the App's `run-status` encoding) or return `null`.
     *
     * The host re-decodes the same text strictly, so a malformed frame fails there with the
     * field named; here a frame that does not decode simply does not satisfy a condition.
     */
    private fun decodeStatus(label: String): Map<String, String>? {
        val fields = LinkedHashMap<String, String>()
        for (part in label.split(";")) {
            val trimmed = part.trim()
            val separator = trimmed.indexOf('=')
            if (separator <= 0) return null
            val key = trimmed.substring(0, separator)
            val value = trimmed.substring(separator + 1)
            if (value.isEmpty() || fields.containsKey(key)) return null
            fields[key] = value
        }
        return fields.ifEmpty { null }
    }

    // -- device access --------------------------------------------------------

    private fun find(id: String): UiObject2? = device.findObject(By.res(id))

    private fun labelOf(node: UiObject2?): String {
        if (node == null) return ""
        val text = node.text
        if (!text.isNullOrEmpty()) return text
        return node.contentDescription?.toString() ?: ""
    }

    private fun launchApp() {
        val component = requireArgument("component")
        // Home first for the same reason `activate` does it: a system panel that holds the
        // focus (the shade, a dialog) can swallow the start, which then reads as "the App
        // never launched". Pressing Home terminates nothing.
        device.pressHome()
        Thread.sleep(300)
        device.executeShellCommand("am start -W -n $component")
        val deadline = SystemClock.uptimeMillis() + DEFAULT_TIMEOUT_MS
        while (device.currentPackageName != appPackage) {
            requireBudget(deadline, "launch foreground")
            Thread.sleep(POLL_MS)
        }
    }

    // -- bounded device I/O ---------------------------------------------------

    private fun readShell(path: String): String {
        val descriptor = instrumentation.uiAutomation.executeShellCommand("cat $path")
        return FileInputStream(descriptor.fileDescriptor).use { stream ->
            val out = ByteArrayOutputStream()
            val buffer = ByteArray(64 * 1024)
            var total = 0
            while (true) {
                val read = stream.read(buffer)
                if (read <= 0) break
                total += read
                if (total > MAX_BYTES) throw PlanError("device file exceeds 1 MiB: $path")
                out.write(buffer, 0, read)
            }
            out.toString("UTF-8")
        }
    }

    /** Publish one file whole: a reader never sees a half-written receipt or done marker. */
    private fun publish(name: String, text: String, stage: String) {
        val directory = filesDir()
        val pending = File(directory, "$name.tmp")
        pending.writeText(text, Charsets.UTF_8)
        val file = File(directory, name)
        if (!pending.renameTo(file)) {
            pending.delete()
            throw PlanError("could not publish $name")
        }
        marker(stage, file.absolutePath)
    }

    private fun dumpHierarchy(name: String) {
        val directory = filesDir()
        val pending = File(directory, "$name.tmp")
        try {
            FileOutputStream(pending).use { stream ->
                device.dumpWindowHierarchy(stream)
            }
            val file = File(directory, name)
            if (!pending.renameTo(file)) {
                throw PlanError("could not publish $name")
            }
            marker("hierarchy", file.absolutePath)
        } catch (error: Throwable) {
            pending.delete()
            marker("hierarchy-failed", describe(error))
        }
    }

    /**
     * The App's external files directory: the host reads it back as the shell user, so the
     * scene never depends on `run-as` or on the App being a debug build.
     */
    private fun filesDir(): File =
        instrumentation.targetContext.getExternalFilesDir(null)
            ?: throw PlanError("the App has no external files directory")

    // -- small helpers --------------------------------------------------------

    private fun requireID(step: JSONObject): String {
        val id = step.optString("id")
        if (id.isEmpty()) throw PlanError("step requires `id`: $step")
        return id
    }

    private fun requireArgument(name: String): String =
        argument(name) ?: throw PlanError("$name instrumentation argument is required")

    private fun argument(name: String): String? =
        InstrumentationRegistry.getArguments().getString(name)

    private fun requireBudget(deadline: Long, label: String) {
        if (SystemClock.uptimeMillis() >= deadline) {
            throw PlanError("timed out before $label")
        }
    }

    private fun remaining(deadline: Long): Long =
        maxOf(1L, deadline - SystemClock.uptimeMillis())

    private fun secondsSince(start: Long): Double =
        (SystemClock.uptimeMillis() - start) / 1000.0

    private fun describe(step: JSONObject): String {
        val parts = mutableListOf<String>()
        for (key in listOf("exists", "label", "labelContains", "enabled", "value", "status")) {
            if (!step.has(key)) continue
            val value = step.opt(key) ?: continue
            if (value is JSONObject) {
                parts.add("status=" + value.keys().asSequence().sorted().joinToString(",") {
                    "$it:${value.optString(it)}"
                })
            } else {
                parts.add("$key=$value")
            }
        }
        return parts.ifEmpty { listOf("exists") }.joinToString(" ")
    }

    private fun describe(error: Throwable): String =
        error.message?.let { "${error.javaClass.simpleName}: $it" } ?: error.javaClass.simpleName

    private class PlanError(message: String) : RuntimeException(message)
}
