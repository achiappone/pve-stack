/** Shapes returned by the Proxmox API and the host exporter.
 *
 *  These are hand-written rather than generated: the PVE API has no published
 *  schema, and only a fraction of each response is used here. Everything is
 *  optional because a node under load, or a token without a permission, can
 *  omit fields without erroring. */

export interface NodeMemory { total?: number; used?: number; free?: number; avail?: number }
export interface NodeRootFs { total?: number; used?: number; avail?: number; free?: number }

export interface NodeCpuInfo {
  model?: string;
  cores?: number;
  cpus?: number;
  sockets?: number;
  mhz?: string;
}

export interface NodeStatus {
  cpu?: number;
  loadavg?: string[];
  memory?: NodeMemory;
  swap?: NodeMemory;
  rootfs?: NodeRootFs;
  uptime?: number;
  kversion?: string;
  pveversion?: string;
  cpuinfo?: NodeCpuInfo;
}

export interface StorageEntry {
  storage: string;
  type?: string;
  total?: number;
  used?: number;
  avail?: number;
}

export interface LxcEntry {
  vmid: number;
  name?: string;
  status?: string;
  mem?: number;
  maxmem?: number;
  uptime?: number;
}

export interface DiskEntry {
  devpath?: string;
  /** /dev/disk/by-id path. The only bus hint the API gives: a USB disk's
   *  link starts "usb-", which is how the page tags one. */
  by_id_link?: string;
  /** What claims the disk - "LVM", "BIOS boot", "partitions" - or absent
   *  when nothing does, which is the state a fresh backup drive is in. */
  used?: string;
  model?: string;
  size?: number;
  health?: string;
  type?: string;
  wearout?: number;
}

/* ---- host exporter (vmbr1, read-only + a fixed action allowlist) ---- */

export interface Fan {
  chip: string; id: string; rpm: number;
  /** Ceiling and the EC's current aim. rpm sitting on target is the
   *  firmware controlling the fan, which is the normal state. */
  max?: number | null;
  target?: number | null;
}
export interface Pwm { chip: string; id: string; value: number; enable: string | null }
export interface TempReading { label: string; c: number }

export interface Volume {
  lv: string;
  size_g: number;
  alloc_pct: number;
  pool: string | null;
  error?: string;
}

export interface Battery {
  name: string;
  percent: number | null;
  status: string | null;
  cycles: string | null;
  volts: number | null;
  wh_now?: number;
  wh_full?: number;
}

export interface Charger {
  name: string; volts: number; amps: number; watts: number;
  status: string | null; online: boolean;
}

export interface HostMetrics {
  hwmon: { fans: Fan[]; pwm: Pwm[]; temps: Record<string, TempReading[]> };
  volumes: Volume[];
  network: {
    bridges: Record<string, string[]>;
    links: Record<string, { oper: string; carrier: string }>;
  };
  cpu: {
    governor: string; loadavg: string[]; driver: string;
    available?: string[];
    /** Dell firmware thermal mode - cool/quiet/balanced/performance.
     *  Unrelated to the governor, and the one that moves the fans. */
    profile?: string | null;
    profiles?: string[];
    /** energy_performance_preference - the knob that actually throttles
     *  this CPU. The governor barely moves it. */
    epp?: string | null;
  };
  power?: { battery: Battery | null; ac_online: boolean | null; charger: Charger | null;
            minutes_to_full?: number };
  deploy?: { lines: string[]; running: boolean };
}

/* ---- what this server hands the page ---- */

export interface ServiceEntry {
  ct: number | null;
  name: string;
  url: string;
  desc?: string;
  lan?: boolean;
  host?: boolean;
  external?: boolean;
  self?: boolean;
}

export interface ProbeResult {
  up: boolean;
  code?: number;
  ms: number;
  err?: string;
}

export interface Snapshot {
  ok: true;
  errors: string[];
  services: ServiceEntry[];
  probes: Record<string, ProbeResult>;
  node?: NodeStatus;
  storage?: StorageEntry[];
  lxc?: LxcEntry[];
  disks?: DiskEntry[];
  host?: HostMetrics;
  downs?: Record<string, DownRecord>;
  /** So a deploy can be confirmed from the API, not just by eye. */
  version?: string;
}

export type ActionName =
  | "fstrim" | "ct_reboot" | "ct_start" | "ct_stop"
  | "nic_rejoin" | "governor" | "host_reboot" | "deploy" | "platform_profile"
  // Handled here, never forwarded to the exporter: the counters are this
  // server's own state, so clear_downs is absent from ALLOWED_ACTIONS.
  | "clear_downs";

export interface ActionBody {
  do?: string;
  ct?: string | number;
  value?: string;
  branch?: string;
  confirm?: string;
  target?: string;
}

export interface ActionResult {
  ok: boolean;
  out?: string;
  err?: string;
  rc?: number;
}

/* ---- retained state: thermal history and down counters ---- */

/** One sample of the charted series, keyed by the same labels the Thermals
 *  panel already builds (`"coretemp Package id 0"`, `"fan1"`). Stored one per
 *  line as JSON, so the keys travel with every point and a chip appearing or
 *  disappearing mid-history is not a schema change. */
export interface Sample {
  t: number;                        // epoch seconds
  temps: Record<string, number>;    // degrees C
  fans: Record<string, number>;     // rpm
}

/** A link, bridge or container's outage tally. `since` is when the count was
 *  last cleared, so "3 downs" always has a window attached to it. */
export interface DownRecord {
  downs: number;
  down: boolean;
  lastDown?: number;
  lastUp?: number;
  since: number;
}
