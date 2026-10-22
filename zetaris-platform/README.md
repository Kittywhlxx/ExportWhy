# Zetaris platform

The platform as seven containers on one machine, managed with Docker Compose. No cluster, no build step.

```
postgres --(healthy)--> driver --(healthy)--> api --(healthy)--> ui
     +-- init --(completed)--+                            +----> bootstrap
```

| Service | Ports | What it is |
|---|---|---|
| `postgres` | internal | metastore and audit databases |
| `init` | -- | one-shot: volume ownership and the shared keypair |
| `driver` | 10000, 4040 (local only) | the query engine; creates the organisation and its administrator on first start |
| `api` | 8888, 8889 (local only) | REST API |
| `ui` | 3000 | the web interface |
| `bootstrap` | -- | one-shot: the sample data |

`init` and `bootstrap` finish as `Exited (0)`. That is success; they are setup jobs, not services.

## Install

Ten minutes of work, plus twenty waiting for a download and a first start.

**1. Install Docker.** Docker Desktop on macOS or Windows, `docker-ce` on Linux. In its settings give Docker **8 GB of memory** and 4 CPUs -- the default is usually less, and the query engine will not start without it.

**2. Unzip the download.**

```bash
unzip zetaris-platform.zip
cd zetaris-platform
```

**3. Sign in to the image registry.** Paste the command from the Zetaris Cloud download page — it carries your username and password, so there is nothing to type.

```bash
docker login zetarishackathon.azurecr.io
```

**4. Check the machine can run it.**

```bash
./preflight.sh
```

Fix anything it reports as FAIL before going on — it is checking the things that otherwise fail twenty minutes later with an error that does not mention them, the registry sign-in above among them.

**5. Nothing to configure.** `.env` is already in the folder, already filled in. Sign in later as `admin@zetaris.com` / `Zetaris1@` and change the password from the user menu, top right.

If you would rather set your own password before the first start, edit `ZETARIS_ADMIN_PASSWORD` in `.env` now — at least 8 characters with a lower case letter, an upper case letter, a digit, and one of `$ @ ! % * ? &`. Other punctuation is rejected, and the rejection does not appear until several minutes into the first start.

**6. Download the images.** About 4 GB, so 5-15 minutes:

```bash
docker compose pull
```

**7. Start it.**

```bash
docker compose up -d
```

The first start takes 5-10 minutes. `init` and `bootstrap` ending as `Exited (0)` is success -- they are setup jobs, not services.

**8. Check it worked.**

```bash
./verify.sh
```

Nine checks, ending with a query that travels the whole stack. It also tells you when the slow first-start step has finished.

**9. Open <http://localhost:3000>** and sign in as `admin@zetaris.com` / `Zetaris1@`. Change the password from the user menu, top right, next to Sign out.

### On a server instead

A machine other than your own needs HTTPS: the interface uses browser APIs that exist only in a secure context, and `localhost` is the sole exception. Over plain HTTP anywhere else, sign-in fails with `crypto.randomUUID is not a function`.

Steps 1-4 are the same. Then:

```bash
cp .env.server.example .env
# set ZETARIS_ADMIN_PASSWORD -- there is no default in this file, because it puts the
#   platform on a public address -- and replace <public-ip> in the three addresses with
# this machine's public IP, hyphenated: 54.1.2.3 becomes 54-1-2-3
docker compose pull
docker compose up -d
```

Open 80 and 443 to the internet. Any name ending `.<ip>.sslip.io` resolves through public DNS with no record to create, so Caddy obtains a real certificate on first start, and the platform is at `https://lightning.<public-ip>.sslip.io`.

For a private address that no certificate authority can validate, set `CADDY_GLOBAL_OPTIONS=local_certs`. Caddy signs its own and the browser warns once.

### If something fails

`./verify.sh` names the component. The usual causes:

| What you see | Cause |
|---|---|
| `pull access denied` | step 4 was skipped |
| driver never becomes healthy | Docker has too little memory -- run `./preflight.sh` |
| `crypto.randomUUID is not a function` | reached over plain HTTP at something other than localhost |
| `bootstrap: still running` | not finished yet; it loads the sample data |
| no `NY_TAXI` or `TPCDS_DB` | the machine could not reach S3 -- see `sample-data-seed.log` in the `driver-logs` volume |

## Sample data

The first start registers the same sample datasets a Zetaris Cloud cluster gets, so instructions written for one work on the other. Both are read in place from the public bucket `s3a://zetaris-public-dataset`, so the machine needs outbound internet.

| Database | Tables | Format |
|---|---|---|
| `NY_TAXI` | 15, e.g. `customers`, `trips`, `yellow_taxi`, `taxi_zones` | CSV |
| `TPCDS_DB` | 24, the TPC-DS schema, e.g. `store_sales`, `item`, `date_dim` | Parquet |

Try `SELECT * FROM NY_TAXI.customers LIMIT 10`.

## Sizing

| | Query engine | For |
|---|---|---|
| `.env` | 2 GB, 2 cores | a laptop |
| `.env.server.example` | 4 GB, 4 cores | an instance with room |

The query engine uses exactly the heap it is given, so this is the one choice that decides how fast queries run. **Do not use the server file on a laptop** -- it asks for 6 GB of heap across the driver and API, and on a smaller machine the driver never starts, with a Spark error that says nothing about memory.

## Tuning the query engine

Heap and cores come from `.env`:

| | |
|---|---|
| `ZSPARK_DRIVER_MEM` | how much heap the query engine gets -- the setting that decides query speed |
| `ZSPARK_DRIVER_CORES` | cores it may use |
| `ZSPARK_MASTER_URL` | `local[2]` runs Spark in-process with two threads |

Everything else is in `conf/spark-defaults.conf`, which the driver reads at startup. It ships with the useful settings commented out beside their defaults, so you can see what is available without reading Spark's documentation:

```bash
vi conf/spark-defaults.conf
docker compose restart driver
```

The one worth knowing about is `spark.sql.shuffle.partitions`. Spark defaults to 200, which is sized for a cluster; on two cores that is 200 small tasks queueing, and lowering it near your core count is usually the largest single improvement.

Heap and cores cannot be set in that file. The driver is launched with them on the command line, and a command line wins -- so `spark.driver.memory` there is ignored silently.

## Connecting over JDBC

The query engine listens on port 10000 on this machine. Tools that speak JDBC -- DBeaver, DataGrip, Tableau, your own Java or Python code -- connect to it through the driver in `jdbc/`.

| | |
|---|---|
| Driver file | `jdbc/ndp-jdbc-driver-2.4.3.1-7eff043-driver.jar` |
| Driver class | `com.zetaris.lightning.jdbc.LightningDriver` |
| URL | `jdbc:zetaris:lightning@localhost:10000` |
| User / password | the account you sign in to the web interface with |

- **Use this jar, not a generic Hive or Spark driver.** It is the only driver that understands the `jdbc:zetaris:lightning@` URL, and it bundles everything it needs, so add the one file to your tool and nothing else.
- **In DBeaver:** Database -> Driver Manager -> New, add the jar under Libraries, set the class and URL above, then create a connection with that driver.
- **Only from this machine.** Port 10000 is bound to `127.0.0.1` because the connection is unencrypted. To reach it from elsewhere, tunnel it: `ssh -L 10000:localhost:10000 <server>`, then connect to `localhost:10000` as above.
- **Port 10000 already taken?** Set `DRIVER_PORT` in `.env`, run `docker compose up -d`, and use that port in the URL.
- **Nothing answers until the driver is healthy.** `./verify.sh` tells you when it is.

## Day to day

```bash
docker compose ps
docker compose logs -f driver
docker compose down          # stop, keep the data
docker compose down -v       # stop and delete everything, including the metastore
```

**Back up `.env`.** It holds the field-encryption keypair's password. Without it, stored datasource credentials cannot be recovered.

## Two things worth knowing

**The keypair is generated once.** `init` writes it into a shared volume on first start, and both the driver and the API read the same copy. `docker compose down -v` destroys it along with everything encrypted with it.

**`init` runs as root and `api` has `group_add`.** A new Docker volume belongs to root until something claims it, so one container takes ownership. The driver and the API then run as different users sharing that tree through a common group.
