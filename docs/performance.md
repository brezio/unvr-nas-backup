# Performance impact on the Protect device

One concern with polling a Protect device on a schedule: does it actually affect performance? We profiled a CloudKey Gen2+ (8-core ARM, 3 GB RAM, 5 cameras) while backups ran every minute for five minutes, sampling CPU, memory, disk I/O, swap, and PostgreSQL activity at 10-second intervals.

Short answer: the impact is negligible.

## What happens during a backup cycle

Each backup run has three phases on the Protect device:

1. **Database query** - The NAS opens an SSH connection and runs a `psql` query against the local PostgreSQL instance. The query hits Postgres's buffer cache (99.98% cache hit rate on our test device) and finishes so fast it never appeared in our 10-second sampling window. PostgreSQL backend count didn't budge.

2. **File transfer (SCP)** - This is the heaviest part. The NAS pulls `.ubv` files over SCP. Since OpenSSH 9.0 (April 2022), `scp` uses the SFTP protocol under the hood, so you'll see both `sshd` and `sftp-server` in the process list during transfers even though the command is `scp`. The encryption runs through SSH (not TLS - SSH has its own crypto stack), and the single-threaded SSH connection means all the cipher work lands on one core.

3. **Remux** - Happens on the NAS, not the Protect device. Zero impact.

## Profiling results

Sampled every 10 seconds for 5 minutes on a CloudKey Gen2+ running 5 cameras, backups polling every 1 minute:

```
TIME         %CPU  %IOWT MEM_MB   SWAP  PG_Q PG_BK  DISK%  SSH NOTES
-----------------------------------------------------------------------------------------
01:23:57       9%     0%   1119    938     1    13     0%    2 -
01:24:08       8%     0%   1128    938     1    16     4%    2 -
01:24:18      14%     0%   1118    938     1    11     5%    2 -
01:24:29      17%     0%   1118    938     1    11     0%    2 -
01:24:39      10%     0%   1120    938     1    11     0%    2 -
01:24:50      12%     0%   1118    938     1    12     0%    2 -
01:25:00      15%     0%   1141    938     1    19     2%    2 -
01:25:11       7%     0%   1121    938     1    11     1%    2 -
01:25:21       8%     0%   1124    938     1    12     0%    2 -
01:25:32       9%     0%   1118    938     1    11     0%    2 -
01:25:42       5%     0%   1119    938     1    11     1%    2 -
01:25:53       6%     0%   1118    938     1    11     1%    2 -
01:26:03      28%     0%   1133    939     1    16    32%    3 SCP     <-- backup run
01:26:14      20%     0%   1121    940     1    11    29%    3 SCP
01:26:25      11%     0%   1119    941     1    12     0%    2 -
01:26:35      10%     0%   1117    941     1    11     0%    2 -
01:26:46       9%     0%   1116    941     1    11     0%    2 -
01:26:56       6%     0%   1122    941     1    13     0%    2 -
01:27:07      26%     0%   1137    941     1    16    28%    3 SCP     <-- backup run
01:27:17      20%     0%   1123    941     1    12    31%    3 SCP
01:27:28      20%     0%   1120    941     1    12     0%    2 -
01:27:38      17%     0%   1124    940     1    11     2%    2 -
01:27:49      23%     0%   1126    940     1    12     2%    2 -
01:27:59      18%     0%   1143    940     1    16     1%    2 -
01:28:10      10%     0%   1132    940     1    15     0%    2 -
01:28:20       7%     0%   1124    940     1    12     2%    2 -
01:28:31      27%     0%   1118    940     1    11     0%    2 -
01:28:41       7%     0%   1118    940     1    11     0%    2 -
01:28:52       8%     0%   1124    940     1    12     1%    2 -
01:29:02       6%     0%   1137    940     1    16     1%    2 -
```

Columns: overall CPU usage, I/O wait percentage, used memory (MB), swap usage (MB), active PostgreSQL queries, PostgreSQL backends, disk utilization on the recording drive, SSH session count, and detected backup activity.

During SCP transfers, CPU rises from a baseline of ~10% to ~20-28% (one core handling SSH encryption) and disk utilization spikes to ~30%. Memory, swap, I/O wait, and PostgreSQL activity are all unchanged. The device sits at 70-90% CPU idle throughout, even at this aggressive 1-minute polling interval.

## Cipher optimization

The default SSH cipher negotiated between most systems is ChaCha20-Poly1305, a software-only stream cipher. Both the CloudKey Gen2+ and UCG-Fiber have ARMv8 hardware AES extensions (`aes` flag in `/proc/cpuinfo`), which means AES-128-GCM can be offloaded to dedicated silicon instead of running in software.

We benchmarked four SCP modes by transferring a 1 GB `.ubv` file from the CloudKey to the NAS (the real backup path) while sampling CPU on the CloudKey every 0.5 seconds:

| Mode | Transfer time | Peak CPU | Total CPU-time |
|---|---|---|---|
| SFTP + ChaCha20 (default) | ~22s | ~25% | Higher |
| SFTP + AES-128-GCM | ~16s | ~25% | **~27% less** |
| Legacy SCP + ChaCha20 | ~22s | ~25% | Higher |
| Legacy SCP + AES-128-GCM | ~16s | ~25% | ~27% less |

Peak CPU percentage is similar across all modes (the cipher work still lands on one core), but AES-GCM finishes ~27% faster, so the total CPU-seconds burned per transfer is significantly lower. The SFTP-vs-legacy-SCP distinction makes no meaningful difference.

Based on this, the project defaults to `-c aes128-gcm@openssh.com` in its SSH options. Both tested Protect devices (CloudKey Gen2+ and UCG-Fiber) confirmed hardware AES support via `/proc/cpuinfo` and successful `aes128-gcm@openssh.com` cipher negotiation (verified with `ssh -v`).

## Tested devices

| Device | CPU | Hardware AES | OpenSSH |
|---|---|---|---|
| CloudKey Gen2+ | AArch64 (Cortex-A53), 8 cores | Yes | 8.4p1 |
| UCG-Fiber | AArch64, 8 cores | Yes | 8.4p1 |

## Bottom line

Even at 1-minute polling (far more aggressive than the default 15-minute schedule), the Protect device handles it comfortably. The database query is invisible, and the file transfer is the only measurable load - brief, bounded to one core, and further reduced by the AES-GCM cipher optimization. The batch throttling settings (`BATCH_SIZE` and `BATCH_DELAY`) provide additional control if needed, but for most setups the defaults are more than conservative enough.
