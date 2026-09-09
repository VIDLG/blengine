import { useEffect, useMemo, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import {
  Badge,
  Button,
  Card,
  Collapsible,
  Divider,
  Grid,
  Heading,
  Stack,
  Text,
} from "@astryxdesign/core";
import {
  colorVars,
  radiusVars,
  spacingVars,
  typographyVars,
} from "@astryxdesign/core/theme/tokens.stylex";

const initialSteps = [
  "initialize", "scan_start", "scan_stop", "connect", "gatt_discovery", "read",
  "write_with_response", "write_without_response", "notifications_enable", "rssi",
  "notifications_disable", "disconnect",
];

const ANDROID_PERIPHERAL_SERVICE_UUID = "fff00000-0000-4000-8000-000000000000";
const ANDROID_PERIPHERAL_CHARACTERISTIC_UUID = "fff10000-0000-4000-8000-000000000000";
const ANDROID_PERIPHERAL_NOTIFICATION_CHARACTERISTIC_UUID = "fff20000-0000-4000-8000-000000000000";
const FLOW_IDS = {
  scanSession: 101,
  connection: 301,
  gattSession: 401,
  subscription: 801,
};

const styles = stylex.create({
  root: {
    minHeight: "100vh",
    backgroundColor: colorVars["--color-background-body"],
    color: colorVars["--color-text-primary"],
    fontFamily: typographyVars["--font-family-body"],
    padding: spacingVars["--spacing-9"],
    "@media (max-width: 820px)": { padding: spacingVars["--spacing-5"] },
  },
  container: { width: "100%", maxWidth: 1120, marginInline: "auto" },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: spacingVars["--spacing-8"],
    "@media (max-width: 820px)": { display: "block" },
  },
  status: {
    minWidth: 220,
    borderInlineStartWidth: 2,
    borderInlineStartStyle: "solid",
    borderInlineStartColor: colorVars["--color-border-green"],
    borderRadius: radiusVars["--radius-inner"],
    paddingBlock: spacingVars["--spacing-3"],
    paddingInlineStart: spacingVars["--spacing-4"],
    "@media (max-width: 820px)": { marginBlockStart: spacingVars["--spacing-6"] },
  },
  actions: { marginBlock: spacingVars["--spacing-7"] },
  cardHeading: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    gap: spacingVars["--spacing-3"],
  },
  list: { width: "100%" },
  stepRow: {
    display: "grid",
    gridTemplateColumns: "30px minmax(0, 1fr) auto",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    paddingBlock: spacingVars["--spacing-2"],
    borderBlockEndWidth: 1,
    borderBlockEndStyle: "solid",
    borderBlockEndColor: colorVars["--color-border"],
  },
  eventRow: {
    display: "grid",
    gridTemplateColumns: "190px minmax(0, 1fr) auto",
    alignItems: "start",
    gap: spacingVars["--spacing-3"],
    paddingBlock: spacingVars["--spacing-2"],
    borderBlockEndWidth: 1,
    borderBlockEndStyle: "solid",
    borderBlockEndColor: colorVars["--color-border"],
  },
  eventList: { maxHeight: 510, overflowY: "auto" },
  eventDetail: {
    marginBlock: 0,
    padding: spacingVars["--spacing-3"],
    overflowX: "auto",
    borderRadius: radiusVars["--radius-inner"],
    backgroundColor: colorVars["--color-background-code"],
    color: colorVars["--color-text-primary"],
    fontFamily: typographyVars["--font-family-code"],
    fontSize: typographyVars["--font-size-supporting"],
    whiteSpace: "pre-wrap",
  },
  footer: { marginBlockStart: spacingVars["--spacing-4"] },
  code: { fontFamily: typographyVars["--font-family-code"] },
});

function tauriApi() {
  const bridge = window.__TAURI__;
  return bridge ? { invoke: bridge.core.invoke, listen: bridge.event.listen } : null;
}

function statusVariant(status) {
  if (status.includes("failed")) return "error";
  if (status === "basic flow passed" || status === "CoreBluetooth flow passed") return "success";
  if (status === "disposed") return "warning";
  return "neutral";
}

export default function App() {
  const [status, setStatus] = useState("ready");
  const [steps, setSteps] = useState([]);
  const [events, setEvents] = useState([]);
  const [running, setRunning] = useState(false);
  const [disposed, setDisposed] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const [flowBlocked, setFlowBlocked] = useState(false);
  const [backendMode, setBackendMode] = useState("deterministic-fake");
  const disposing = useRef(false);
  const api = useMemo(tauriApi, []);
  const eventWaiters = useRef([]);

  useEffect(() => {
    if (!api) {
      setStatus("launch with Tauri host");
      return undefined;
    }
    let active = true;
    void api.invoke("plugin:blehub|backend_mode").then((mode) => {
      if (active && mode) setBackendMode(mode);
    }).catch(() => {});
    const unsubscribe = api.listen("blehub://event", ({ payload }) => {
      if (!active) return;
      setEvents((current) => [...current, payload]);
      if (payload.kind === "adapter_snapshot_changed") {
        setInitialized(true);
        setStatus(payload.detail ?? "adapter ready");
      }
      eventWaiters.current = eventWaiters.current.filter((waiter) => {
        if (!waiter.predicate(payload)) return true;
        clearTimeout(waiter.timer);
        waiter.resolve(payload);
        return false;
      });
    });
    return () => {
      active = false;
      Promise.resolve(unsubscribe).then((stop) => {
        if (!active) stop?.();
      });
      eventWaiters.current.forEach((waiter) => {
        clearTimeout(waiter.timer);
        waiter.reject(new Error("event listener disposed"));
      });
      eventWaiters.current = [];
    };
  }, [api]);

  function rejectPendingEventWaiters(reason) {
    const waiters = eventWaiters.current;
    eventWaiters.current = [];
    waiters.forEach((waiter) => {
      clearTimeout(waiter.timer);
      waiter.reject(reason);
    });
  }

  function waitForEvent(predicate, timeout = 30_000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        eventWaiters.current = eventWaiters.current.filter((waiter) => waiter.timer !== timer);
        reject(new Error("timed out waiting for BleHub event"));
      }, timeout);
      eventWaiters.current.push({ predicate, resolve, reject, timer });
    });
  }

  function commandEvent(commandId) {
    return (event) => event.kind === "command_completed"
      && event.command_id === commandId
      && event.detail == null;
  }

  async function invokeStep(command, args, predicates) {
    setStatus(`running CoreBluetooth flow: ${command}`);
    const waits = predicates.map((predicate) => waitForEvent(predicate));
    try {
      await api.invoke(`plugin:blehub|${command}`, args);
      return await Promise.all(waits);
    } catch (error) {
      waits.forEach((wait) => void wait.catch(() => {}));
      throw error;
    }
  }

  async function runCoreBluetoothFlow() {
    if (!api || disposed) return;
    setRunning(true);
    setStatus("running CoreBluetooth flow: scan");
    setSteps([]);
    setEvents([]);
    const completed = [];
    const mark = (name, result) => {
      setStatus(`running CoreBluetooth flow: ${name}`);
      completed.push({ name, event_count: result.length });
      setSteps([...completed]);
    };
    try {
      // The flow button is only available after initialization has completed;
      // do not dispatch initialize a second time on the same Apple backend.
      mark("initialize", []);

      const scanEvents = await invokeStep("start_scan", {
        request: {
          command_id: 2,
          scan_session_id: FLOW_IDS.scanSession,
          service_uuids: [ANDROID_PERIPHERAL_SERVICE_UUID],
          name_prefix: null,
          company_id: null,
          allow_duplicates: true,
        },
      }, [
        commandEvent(2),
        (event) => event.kind === "advertisement_observed"
          && event.data?.scan_session_id === FLOW_IDS.scanSession
          && event.data.advertised_services?.includes(ANDROID_PERIPHERAL_SERVICE_UUID),
      ]);
      const advertisement = scanEvents.find((event) => event.kind === "advertisement_observed");
      mark("scan_start", scanEvents);

      const stoppedEvents = await invokeStep("stop_scan", {
        request: { command_id: 3, scan_session_id: FLOW_IDS.scanSession },
      }, [
        commandEvent(3),
        (event) => event.kind === "scan_stopped" && event.data?.scan_session_id === FLOW_IDS.scanSession,
      ]);
      mark("scan_stop", stoppedEvents);

      const connectedEvents = await invokeStep("connect", {
        request: {
          command_id: 4,
          connection_id: FLOW_IDS.connection,
          peripheral_id: advertisement.data.peripheral_id,
        },
      }, [
        commandEvent(4),
        (event) => event.kind === "connection_state_changed"
          && event.command_id === 4
          && event.data?.connection_id === FLOW_IDS.connection
          && event.data.state?.includes("Connected"),
      ]);
      mark("connect", connectedEvents);

      const discoveredEvents = await invokeStep("discover_gatt", {
        request: {
          command_id: 5,
          connection_id: FLOW_IDS.connection,
          gatt_session_id: FLOW_IDS.gattSession,
        },
      }, [commandEvent(5), (event) => event.kind === "gatt_discovered" && event.command_id === 5]);
      const discovered = discoveredEvents.find((event) => event.kind === "gatt_discovered");
      const characteristics = discovered.data?.services?.flatMap((service) => service.characteristics) ?? [];
      const characteristic = characteristics.find(
        (candidate) => candidate.uuid === ANDROID_PERIPHERAL_CHARACTERISTIC_UUID,
      );
      const notificationCharacteristic = characteristics.find(
        (candidate) => candidate.uuid === ANDROID_PERIPHERAL_NOTIFICATION_CHARACTERISTIC_UUID,
      );
      if (!characteristic) throw new Error("fixture read/write characteristic was not present in GATT database");
      if (!notificationCharacteristic) throw new Error("fixture notification characteristic was not present in GATT database");
      mark("gatt_discovery", discoveredEvents);

      const readEvents = await invokeStep("read_characteristic", {
        request: {
          command_id: 6,
          connection_id: FLOW_IDS.connection,
          gatt_session_id: FLOW_IDS.gattSession,
          characteristic_id: characteristic.id,
        },
      }, [commandEvent(6), (event) => event.kind === "characteristic_read" && event.command_id === 6]);
      mark("read", readEvents);

      const writeResponseEvents = await invokeStep("write_characteristic", {
        request: {
          command_id: 7,
          connection_id: FLOW_IDS.connection,
          gatt_session_id: FLOW_IDS.gattSession,
          characteristic_id: characteristic.id,
          value: [0x10, 0x20, 0x30],
          write_type: "with_response",
        },
      }, [commandEvent(7), (event) => event.kind === "characteristic_written" && event.command_id === 7]);
      mark("write_with_response", writeResponseEvents);

      const writeWithoutResponseEvents = await invokeStep("write_characteristic", {
        request: {
          command_id: 8,
          connection_id: FLOW_IDS.connection,
          gatt_session_id: FLOW_IDS.gattSession,
          characteristic_id: characteristic.id,
          value: [0xab, 0xcd],
          write_type: "without_response",
        },
      }, [commandEvent(8)]);
      mark("write_without_response", writeWithoutResponseEvents);

      const notificationEvents = await invokeStep("set_notifications", {
        request: {
          command_id: 9,
          connection_id: FLOW_IDS.connection,
          gatt_session_id: FLOW_IDS.gattSession,
          subscription_id: FLOW_IDS.subscription,
          characteristic_id: notificationCharacteristic.id,
          action: "enable",
          mode: "platform_default",
        },
      }, [
        commandEvent(9),
        (event) => event.kind === "notification_state_changed"
          && event.command_id === 9
          && event.data?.state?.includes("Enabled"),
        (event) => event.kind === "notification_received"
          && event.data?.subscription_id === FLOW_IDS.subscription,
      ]);
      mark("notifications_enable", notificationEvents);

      const rssiEvents = await invokeStep("read_rssi", {
        request: { command_id: 10, connection_id: FLOW_IDS.connection },
      }, [commandEvent(10), (event) => event.kind === "rssi_read" && event.command_id === 10]);
      mark("rssi", rssiEvents);

      const disabledEvents = await invokeStep("set_notifications", {
        request: {
          command_id: 11,
          connection_id: FLOW_IDS.connection,
          gatt_session_id: FLOW_IDS.gattSession,
          subscription_id: FLOW_IDS.subscription,
          characteristic_id: notificationCharacteristic.id,
          action: "disable",
          mode: null,
        },
      }, [
        commandEvent(11),
        (event) => event.kind === "notification_state_changed"
          && event.command_id === 11
          && event.data?.state?.includes("Disabled"),
      ]);
      mark("notifications_disable", disabledEvents);

      const disconnectedEvents = await invokeStep("disconnect", {
        request: { command_id: 12, connection_id: FLOW_IDS.connection },
      }, [
        commandEvent(12),
        (event) => event.kind === "connection_state_changed"
          && event.command_id === 12
          && event.data?.state?.includes("Disconnected"),
      ]);
      mark("disconnect", disconnectedEvents);
      setStatus("CoreBluetooth flow passed");
    } catch (error) {
      if (!disposing.current) {
        setFlowBlocked(true);
        setStatus(`flow failed: ${String(error)}`);
      }
    } finally {
      setRunning(false);
    }
  }

  async function initializeAdapter() {
    if (!api) return;
    setRunning(true);
    setStatus("initializing CoreBluetooth");
    setSteps([]);
    setEvents([]);
    try {
      await api.invoke("plugin:blehub|initialize", { commandId: 1 });
      setStatus("initialize dispatched; waiting for adapter state");
    } catch (error) {
      setStatus(`failed: ${String(error)}`);
    } finally {
      setRunning(false);
    }
  }

  async function runBasicProbe() {
    if (!api) return;
    setRunning(true);
    setStatus("running");
    setSteps([]);
    setEvents([]);
    try {
      const result = await api.invoke("plugin:blehub|run_basic_probe");
      setSteps(result.steps);
      setStatus("basic flow passed");
    } catch (error) {
      setStatus(`failed: ${String(error)}`);
    } finally {
      setRunning(false);
    }
  }

  async function disposeHub() {
    if (!api || disposed) return;
    disposing.current = true;
    rejectPendingEventWaiters(new Error("hub is being disposed"));
    setRunning(false);
    try {
      await api.invoke("plugin:blehub|close");
      setDisposed(true);
      setStatus("disposed");
    } catch (error) {
      disposing.current = false;
      setStatus(`dispose failed: ${String(error)}`);
    }
  }

  const displayedSteps = steps.length ? steps : initialSteps.map((name) => ({ name, event_count: 0 }));
  return (
    <main {...stylex.props(styles.root)}>
      <Stack gap={8} xstyle={styles.container}>
        <header {...stylex.props(styles.header)}>
          <Stack gap={2}>
            <Text type="supporting" color="accent">BLEHUB / TAURI</Text>
            <Heading level={1}>Central contract lab</Heading>
            <Text type="body" color="secondary" display="block">
              A visible React surface for the selected BleHub backend and event stream.
            </Text>
          </Stack>
          <Stack gap={2} xstyle={styles.status}>
            <Text type="supporting" color="secondary">Runtime status</Text>
            <Badge variant={statusVariant(status)} label={status} />
          </Stack>
        </header>

        <Stack gap={2} xstyle={styles.actions}>
          <Stack direction="horizontal" gap={2} wrap="wrap">
            {backendMode === "apple-corebluetooth" && initialized ? (
              <Badge variant="success" label="CoreBluetooth ready" />
            ) : (
              <Button
                variant="primary"
                onClick={backendMode === "apple-corebluetooth" ? initializeAdapter : runBasicProbe}
                disabled={!api || running || disposed}
              >
                {running
                  ? status.startsWith("running CoreBluetooth flow:")
                    ? status.replace("running CoreBluetooth flow: ", "Running: ")
                    : "Running..."
                  : backendMode === "apple-corebluetooth" ? "Initialize CoreBluetooth" : "Run basic flow"}
              </Button>
            )}
            {backendMode === "apple-corebluetooth" && initialized && !disposed && (
              <Button
                variant="secondary"
                onClick={runCoreBluetoothFlow}
                disabled={!api || running || flowBlocked}
              >
                {flowBlocked
                  ? "Flow failed — dispose to retry"
                  : steps.length === initialSteps.length ? "Run CoreBluetooth flow again" : "Run CoreBluetooth flow"}
              </Button>
            )}
            <Button variant="secondary" onClick={disposeHub} disabled={!api || disposed}>
              Dispose hub
            </Button>
          </Stack>
          <Text type="supporting" color="secondary" display="block">
            {backendMode === "apple-corebluetooth" && initialized
              ? "CoreBluetooth ready. Run the flow again after a clean disconnect; close the hub to end this session."
              : "Initialize CoreBluetooth once to begin. Close the hub to end this session."}
          </Text>
        </Stack>

        <Grid columns={{ minWidth: 320, repeat: "fit" }} gap={4}>
          <Card padding={4} elevation="low">
            <Stack gap={3}>
              <Stack direction="horizontal" justify="between" align="center" xstyle={styles.cardHeading}>
                <Heading level={2}>Contract coverage</Heading>
                <Badge variant={steps.length ? "success" : "neutral"} label={`${steps.length}/12`} />
              </Stack>
              <Divider />
              <Stack gap={0} xstyle={styles.list}>
                {displayedSteps.map((step) => (
                  <div {...stylex.props(styles.stepRow)} key={step.name}>
                    <Badge variant={steps.length ? "success" : "neutral"} label={steps.length ? "OK" : "--"} />
                    <Text type="body">{step.name}</Text>
                    <Text type="supporting" color="secondary">{step.event_count} events</Text>
                  </div>
                ))}
              </Stack>
            </Stack>
          </Card>

          <Card padding={4} elevation="low">
            <Stack gap={3}>
              <Stack direction="horizontal" justify="between" align="center" xstyle={styles.cardHeading}>
                <Heading level={2}>Projected events</Heading>
                <Badge variant="info" label={events.length} />
              </Stack>
              <Divider />
              <Stack gap={0} xstyle={styles.eventList}>
                {events.length ? events.map((event, index) => {
                  const hasData = event.data !== undefined && event.data !== null;
                  const rowContent = (
                    <>
                      <Text type="code" xstyle={styles.code}>{event.kind}</Text>
                      <Text type="supporting" color="secondary">{event.detail ?? "terminal event"}</Text>
                      {hasData && <Text type="supporting" color="secondary">data</Text>}
                    </>
                  );
                  if (hasData) {
                    return (
                      <Collapsible
                        key={`${event.kind}-${index}`}
                        trigger={<span {...stylex.props(styles.eventRow)}>{rowContent}</span>}
                        defaultIsOpen={false}
                      >
                        <pre {...stylex.props(styles.eventDetail)}>{JSON.stringify(event.data, null, 2)}</pre>
                      </Collapsible>
                    );
                  }
                  return <div key={`${event.kind}-${index}`} {...stylex.props(styles.eventRow)}>{rowContent}</div>;
                }) : (
                  <Text type="supporting" color="secondary" display="block">
                    Run the flow to inspect domain and terminal events.
                  </Text>
                )}
              </Stack>
            </Stack>
          </Card>
        </Grid>

        <Text type="supporting" color="secondary" xstyle={styles.footer}>
          Backend path: {backendMode}. Event results are surfaced through the typed BleHub contract.
        </Text>
      </Stack>
    </main>
  );
}
