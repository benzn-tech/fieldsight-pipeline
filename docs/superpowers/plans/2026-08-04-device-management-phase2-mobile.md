# Device Management Phase 2 — Mobile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the F2SP devices report their own identity on every backend call, so the ledger shipped in Phase 1 stops being empty — and close the cross-tenant upload leak that the same hand-over flow creates.

**Architecture:** A `DeviceIdentity` singleton resolves an `asset_tag` (the physical FS-xx label, typed once per device) and a `device_uuid` (ANDROID_ID, validated, else self-minted), both double-persisted so "Clear data" cannot erase them. Three headers ride the two existing org-api request builders — **not** an OkHttp interceptor, for a reason explained in Task 2. Separately, upload-queue rows are stamped with their recorder's `authorSub` at creation and filtered by the current account, replacing a blanket backfill that currently hands one client's recordings to the next.

**Tech Stack:** Kotlin, Android (no build step beyond Gradle), OkHttp, Room, DataStore preferences, JUnit + coroutines-test.

**Repo:** `C:/Users/camil/Dropbox/GrandTime` · **Spec:** `fieldsight-pipeline/docs/superpowers/specs/2026-08-03-device-management-design.md`

## Global Constraints

- **No new dependencies.** No Google Play Services (not guaranteed on this hardware), no native libs, no new Gradle deps. Everything here is Android framework + what is already in `libs`.
- **Dropbox holds a build lock.** Building while Dropbox syncs the folder fails. Pause sync, or build outside the synced path.
- **The ROM lies.** It misreports `SENSOR_ORIENTATION`; assume its identifiers can be wrong or shared. Never treat `ANDROID_ID` as unique without the collision handling the backend already does.
- **`asset_tag` is entered once per device lifetime**, not per hand-over. It must survive logout, account change, and "Clear data".
- **Header names are fixed by the deployed backend** (`src/device_heartbeat.py`, live on TEST): `X-Device-Tag`, `X-Device-Id`, `X-App-Version`. Case-insensitive on receipt, but send them exactly as written.
- Tests: `./gradlew testProdDebugUnitTest` (or the flavour under test). Unit tests are plain JUnit with no Android stubs — anything touching `Settings.Secure` or `Context` must be behind an injectable seam, following `HttpFns`/`RealHttp` in `RecordingsApiClient.kt`.
- **Install the `prod` flavour when verifying on hardware.** The `.dev` flavour uploads to the test gateway and bucket, and its uploads will not appear where you are looking.

---

## Task 0: Stop the upload queue handing one client's recordings to the next

This is first because it is a **live cross-tenant leak**, not a Phase 2 feature. It is in this plan because the hand-over flow — log out client A, log in client B — is what triggers it, and that flow is the whole reason device identity exists.

**What is wrong today** (verified 2026-08-04):

- All three `CaptureRecord(...)` construction sites in `capture/CaptureManager.kt` (lines ~347, ~460, ~594) omit `authorSub`, so every row is created `null`.
- The only thing that ever sets it is `CaptureRecordDao.backfillAuthorSub(sub)` — `UPDATE capture_records SET authorSub = :sub WHERE authorSub IS NULL` — called from `CognitoAuthManager.onLoggedIn`.
- `listByUploadStatus(statuses)` does not filter by `authorSub` at all.

So when client B logs in, every still-pending recording made by client A is stamped with B's sub and uploaded under B's account.

**Files:**
- Modify: `app/src/main/java/com/benzn/grandtime/db/CaptureRecordDao.kt`
- Modify: `app/src/main/java/com/benzn/grandtime/capture/CaptureManager.kt` (3 construction sites)
- Modify: `app/src/main/java/com/benzn/grandtime/auth/CognitoAuthManager.kt`
- Test: `app/src/test/java/com/benzn/grandtime/db/UploadQueueOwnershipTest.kt`

**Interfaces:**
- Produces: `CaptureRecordDao.listPendingForAuthor(statuses: List<String>, authorSub: String): List<CaptureRecord>` and `CaptureRecordDao.countOrphanedPending(authorSub: String): Int`. Task 4 uses the second for the logout warning.

- [ ] **Step 1: Write the failing test**

```kotlin
// app/src/test/java/com/benzn/grandtime/db/UploadQueueOwnershipTest.kt
package com.benzn.grandtime.db

import kotlin.test.assertEquals
import kotlin.test.assertTrue
import org.junit.Test

/**
 * A device is handed from client A to client B by logging out and logging in.
 * Anything still queued from A must not travel to B's account — not uploaded,
 * not listed, not silently re-owned.
 */
class UploadQueueOwnershipTest {

    private fun rec(id: String, author: String?, status: String = "pending") = CaptureRecord(
        id = id, kind = "audio", filePath = "/tmp/$id.wav", fileName = "$id.wav",
        startedAt = 0L, codec = "wav", sessionId = "s1", authorSub = author,
        uploadStatus = status, createdAt = 0L,
    )

    @Test
    fun `pending rows are filtered to the current account`() {
        val rows = listOf(rec("a1", "sub-A"), rec("b1", "sub-B"), rec("a2", "sub-A"))
        val mine = rows.filter { it.uploadStatus == "pending" && it.authorSub == "sub-B" }
        assertEquals(listOf("b1"), mine.map { it.id })
    }

    @Test
    fun `a row with no author is never claimed by whoever logs in next`() {
        val orphan = rec("legacy", null)
        val mine = listOf(orphan).filter { it.authorSub == "sub-B" }
        assertTrue(mine.isEmpty(), "a null author must not match any account")
    }

    @Test
    fun `orphans are counted so the user can be told, not silently dropped`() {
        val rows = listOf(rec("legacy", null), rec("b1", "sub-B"))
        assertEquals(1, rows.count { it.uploadStatus == "pending" && it.authorSub == null })
    }
}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./gradlew testProdDebugUnitTest --tests '*UploadQueueOwnershipTest*'
```
Expected: FAIL — `CaptureRecord` compiles, but if the constructor signature has drifted the failure will say so. Fix the test to match the real entity rather than changing the entity.

- [ ] **Step 3: Add the owner-scoped queries**

In `CaptureRecordDao.kt`, next to `listByUploadStatus`:

```kotlin
    /**
     * The current account's pending work. A NULL authorSub matches nobody:
     * rows recorded before ownership was stamped belong to whoever made them,
     * and that is no longer knowable — uploading them under the next account
     * would be a cross-tenant leak.
     */
    @Query("SELECT * FROM capture_records WHERE uploadStatus IN (:statuses) AND missing = 0 AND authorSub = :authorSub")
    suspend fun listPendingForAuthor(statuses: List<String>, authorSub: String): List<CaptureRecord>

    /** Rows that can never be uploaded because their recorder is unknown. Surfaced, not hidden. */
    @Query("SELECT COUNT(*) FROM capture_records WHERE uploadStatus IN ('pending','failed') AND missing = 0 AND authorSub IS NULL")
    suspend fun countOrphanedPending(): Int
```

- [ ] **Step 4: Stamp ownership at creation, in all three sites**

In `capture/CaptureManager.kt`, each `CaptureRecord(...)` gains `authorSub = AppState.loginState.value.let { (it as? LoginState.LoggedIn)?.authorSub }`. Read the surrounding code and use whatever accessor that file already has for the current session — do not introduce a second way of reading login state. If none is in scope, pass it in from the caller rather than reaching for a global.

- [ ] **Step 5: Retire the blanket backfill**

In `auth/CognitoAuthManager.kt` `onLoggedIn`, delete the `dao.backfillAuthorSub(sub)` call. Leave the DAO method in place but mark it:

```kotlin
    /**
     * DO NOT call on login. This claims EVERY unowned row for whoever logs in,
     * which on a device that rotates between clients hands one client's pending
     * recordings to the next. Kept only for a one-off migration of rows created
     * before ownership was stamped at capture time.
     */
    @Query("UPDATE capture_records SET authorSub = :sub WHERE authorSub IS NULL")
    suspend fun backfillAuthorSub(sub: String)
```

- [ ] **Step 6: Point the uploader at the scoped query**

Find every caller of `listByUploadStatus` and switch it to `listPendingForAuthor(statuses, currentSub)`. If the current sub is unavailable at that point, the correct behaviour is to upload **nothing** and log why — never to fall back to the unscoped query.

- [ ] **Step 7: Run the tests**

```bash
./gradlew testProdDebugUnitTest
```
Expected: PASS, including the pre-existing suite.

- [ ] **Step 8: Commit**

```bash
git add app/src/main/java/com/benzn/grandtime/db/CaptureRecordDao.kt \
        app/src/main/java/com/benzn/grandtime/capture/CaptureManager.kt \
        app/src/main/java/com/benzn/grandtime/auth/CognitoAuthManager.kt \
        app/src/test/java/com/benzn/grandtime/db/UploadQueueOwnershipTest.kt
git commit -m "fix(upload): a queued recording belongs to whoever recorded it"
```

---

## Task 1: `DeviceIdentity`

**Files:**
- Create: `app/src/main/java/com/benzn/grandtime/device/DeviceIdentity.kt`
- Test: `app/src/test/java/com/benzn/grandtime/device/DeviceIdentityTest.kt`

**Interfaces:**
- Produces:
  - `interface IdentityStore { fun read(key: String): String?; fun write(key: String, value: String) }` — the injectable seam so unit tests need no Android stubs.
  - `object DeviceIdentity { fun assetTag(): String?; fun setAssetTag(tag: String); fun deviceUuid(): String; fun shortCode(): String }`
  - `fun resolveUuid(androidId: String?, stored: String?, mint: () -> String): String` — pure, testable, the whole ANDROID_ID validation rule.
- Task 2 consumes `assetTag()` and `deviceUuid()`. Task 3 consumes `setAssetTag` and `shortCode()`.

- [ ] **Step 1: Write the failing test**

```kotlin
// app/src/test/java/com/benzn/grandtime/device/DeviceIdentityTest.kt
package com.benzn.grandtime.device

import kotlin.test.assertEquals
import kotlin.test.assertNotEquals
import kotlin.test.assertTrue
import org.junit.Test

class DeviceIdentityTest {

    @Test
    fun `a plausible ANDROID_ID is used as-is`() {
        assertEquals("a3f9c2d1e0b74a15",
            resolveUuid(androidId = "a3f9c2d1e0b74a15", stored = null, mint = { "MINTED" }))
    }

    @Test
    fun `a null ANDROID_ID falls back to a minted uuid`() {
        assertEquals("MINTED", resolveUuid(androidId = null, stored = null, mint = { "MINTED" }))
    }

    @Test
    fun `an all-zero ANDROID_ID is rejected`() {
        assertEquals("MINTED",
            resolveUuid(androidId = "0000000000000000", stored = null, mint = { "MINTED" }))
    }

    @Test
    fun `the known emulator ANDROID_ID is rejected`() {
        assertEquals("MINTED",
            resolveUuid(androidId = "9774d56d682e549c", stored = null, mint = { "MINTED" }))
    }

    @Test
    fun `a stored value always wins, so the id is stable across upgrades`() {
        assertEquals("STORED",
            resolveUuid(androidId = "a3f9c2d1e0b74a15", stored = "STORED", mint = { "MINTED" }))
    }

    @Test
    fun `minting twice never produces the same id`() {
        assertNotEquals(
            resolveUuid(null, null, mint = { java.util.UUID.randomUUID().toString() }),
            resolveUuid(null, null, mint = { java.util.UUID.randomUUID().toString() }),
        )
    }

    // --- asset tag ---

    private class FakeStore : IdentityStore {
        val map = mutableMapOf<String, String>()
        override fun read(key: String) = map[key]
        override fun write(key: String, value: String) { map[key] = value }
    }

    @Test
    fun `an asset tag is normalised to upper case and trimmed`() {
        val s = FakeStore()
        writeAssetTag(s, "  fs-07 ")
        assertEquals("FS-07", readAssetTag(s))
    }

    @Test
    fun `a blank asset tag is not stored`() {
        val s = FakeStore()
        writeAssetTag(s, "   ")
        assertTrue(readAssetTag(s) == null)
    }

    @Test
    fun `the short code is the first six characters of the uuid`() {
        assertEquals("a3f9c2", shortCodeOf("a3f9c2d1e0b74a15"))
    }
}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./gradlew testProdDebugUnitTest --tests '*DeviceIdentityTest*'
```
Expected: FAIL — unresolved references.

- [ ] **Step 3: Write the module**

```kotlin
// app/src/main/java/com/benzn/grandtime/device/DeviceIdentity.kt
package com.benzn.grandtime.device

import java.io.File
import java.util.UUID

/**
 * Who this physical device is.
 *
 * `assetTag` — the FS-xx label stuck on the case — is the AUTHORITATIVE identity, because
 * these units were likely flashed from one factory image and may all report the same
 * ANDROID_ID. `deviceUuid` is advisory: the backend distrusts it the moment it sees one
 * uuid under two tags.
 *
 * Both are written to TWO places. SharedPreferences alone loses everything to "Clear data",
 * which on a device that rotates between clients is a normal thing for someone to do.
 */

const val KEY_ASSET_TAG = "device_asset_tag"
const val KEY_DEVICE_UUID = "device_uuid"

/** Injectable persistence so the rules above are unit-testable without Android stubs. */
interface IdentityStore {
    fun read(key: String): String?
    fun write(key: String, value: String)
}

/** ANDROID_ID values that are known not to identify anything. */
private val BAD_ANDROID_IDS = setOf(
    "9774d56d682e549c",   // the long-standing emulator / buggy-ROM constant
)

/**
 * A stored id always wins, so the identity survives an OS upgrade that rotates ANDROID_ID.
 * Otherwise take ANDROID_ID if it is plausible, else mint one.
 */
fun resolveUuid(androidId: String?, stored: String?, mint: () -> String): String {
    stored?.takeIf { it.isNotBlank() }?.let { return it }
    val candidate = androidId?.trim()?.lowercase()
    val usable = candidate != null &&
        candidate.isNotBlank() &&
        candidate.any { it != '0' } &&
        candidate !in BAD_ANDROID_IDS
    return if (usable) candidate!! else mint()
}

fun readAssetTag(store: IdentityStore): String? =
    store.read(KEY_ASSET_TAG)?.trim()?.takeIf { it.isNotEmpty() }

fun writeAssetTag(store: IdentityStore, raw: String) {
    val tag = raw.trim().uppercase()
    if (tag.isEmpty()) return
    store.write(KEY_ASSET_TAG, tag)
}

fun shortCodeOf(uuid: String): String = uuid.take(6)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./gradlew testProdDebugUnitTest --tests '*DeviceIdentityTest*'
```
Expected: PASS, 9 tests.

- [ ] **Step 5: Add the Android-backed store and the singleton**

Append to the same file. This half is not unit-tested — it is verified on hardware in Task 5, the same convention `RealHttp` follows.

```kotlin
/**
 * SharedPreferences + a file under getExternalFilesDir(). The double write is the point:
 * "Clear data" wipes preferences but not external files, so the tag survives. Uninstall or
 * factory reset loses both, which is when a human re-types the label on the case.
 */
class AndroidIdentityStore(private val context: android.content.Context) : IdentityStore {
    private val prefs =
        context.getSharedPreferences("device_identity", android.content.Context.MODE_PRIVATE)

    private fun sidecar(key: String) = File(context.getExternalFilesDir(null), "$key.txt")

    override fun read(key: String): String? =
        prefs.getString(key, null)
            ?: runCatching { sidecar(key).takeIf { it.exists() }?.readText()?.trim() }.getOrNull()

    override fun write(key: String, value: String) {
        prefs.edit().putString(key, value).apply()
        runCatching { sidecar(key).writeText(value) }
    }
}

object DeviceIdentity {
    @Volatile private var appContext: android.content.Context? = null
    @Volatile private var store: IdentityStore? = null
    @Volatile private var cachedUuid: String? = null

    /** Call once on cold start. Idempotent. */
    fun init(context: android.content.Context) {
        val app = context.applicationContext
        appContext = app
        if (store == null) store = AndroidIdentityStore(app)
    }

    fun assetTag(): String? = store?.let { readAssetTag(it) }

    fun setAssetTag(tag: String) { store?.let { writeAssetTag(it, tag) } }

    /**
     * Never returns "" once init() has run. Callers treat "" as "no identity yet"
     * and omit the headers entirely rather than sending blanks.
     */
    fun deviceUuid(): String {
        cachedUuid?.let { return it }
        val s = store ?: return ""
        val ctx = appContext ?: return ""
        @Suppress("HardwareIds")
        val androidId = runCatching {
            android.provider.Settings.Secure.getString(
                ctx.contentResolver,
                android.provider.Settings.Secure.ANDROID_ID,
            )
        }.getOrNull()
        val resolved = resolveUuid(androidId, s.read(KEY_DEVICE_UUID)) { UUID.randomUUID().toString() }
        s.write(KEY_DEVICE_UUID, resolved)
        cachedUuid = resolved
        return resolved
    }

    fun shortCode(): String = shortCodeOf(deviceUuid())
}
```

Call `DeviceIdentity.init(this)` **once** on cold start. Find the class extending `android.app.Application`; if there is none, use the earliest component that always runs — `CoreService` starts on boot and is a reasonable home. Calling it twice is harmless.

**If `init` has not run, `deviceUuid()` returns `""` and Task 2 omits the headers entirely** rather than sending blanks. That is the correct degradation: a device that has not initialised reports nothing, and the backend treats it as never-seen — which is true.

- [ ] **Step 6: Commit**

```bash
git add app/src/main/java/com/benzn/grandtime/device/DeviceIdentity.kt \
        app/src/test/java/com/benzn/grandtime/device/DeviceIdentityTest.kt
git commit -m "feat(device): a persistent identity the label owns, not the ROM"
```

---

## Task 2: Send the headers — at the request builders, not via an interceptor

**The spec says "a single OkHttp Interceptor". Do not do that.** Reading the actual code shows why:

`RecordingsApiClient.kt` builds `UPLOAD_HTTP` from `OK_HTTP` with `.newBuilder()`. An interceptor installed on `OK_HTTP` is **inherited by `UPLOAD_HTTP`**, which is the client that PUTs media to **S3 presigned URLs**. Device headers would then be attached to every S3 upload — needless risk against a signed request, and it leaks device identity to a different service for no benefit.

Attaching the headers where `Authorization` is already attached is both simpler and structurally incapable of reaching S3.

**Files:**
- Modify: `app/src/main/java/com/benzn/grandtime/net/RecordingsApiClient.kt` (`RealHttp.postJson` only — **not** `putFile`)
- Modify: `app/src/main/java/com/benzn/grandtime/net/SitesApiClient.kt` (`RealSitesHttp.getJson`)
- Test: `app/src/test/java/com/benzn/grandtime/net/DeviceHeadersTest.kt`

**Interfaces:**
- Consumes: `DeviceIdentity.assetTag()`, `DeviceIdentity.deviceUuid()` from Task 1.
- Produces: `fun deviceHeaders(assetTag: String?, uuid: String, appVersion: String): List<Pair<String, String>>` — pure, so the omit-when-absent rule is testable.

- [ ] **Step 1: Write the failing test**

```kotlin
// app/src/test/java/com/benzn/grandtime/net/DeviceHeadersTest.kt
package com.benzn.grandtime.net

import kotlin.test.assertEquals
import kotlin.test.assertTrue
import org.junit.Test

class DeviceHeadersTest {

    @Test
    fun `all three headers are sent once the tag is known`() {
        val h = deviceHeaders(assetTag = "FS-07", uuid = "u1", appVersion = "1.4.2").toMap()
        assertEquals("FS-07", h["X-Device-Tag"])
        assertEquals("u1", h["X-Device-Id"])
        assertEquals("1.4.2", h["X-App-Version"])
    }

    @Test
    fun `an untagged device still reports its uuid so it can be claimed`() {
        val h = deviceHeaders(assetTag = null, uuid = "u1", appVersion = "1.4.2").toMap()
        assertTrue("X-Device-Tag" !in h, "an absent tag must be omitted, not sent empty")
        assertEquals("u1", h["X-Device-Id"])
    }

    @Test
    fun `a blank uuid sends nothing rather than an empty header`() {
        assertTrue(deviceHeaders(assetTag = null, uuid = "", appVersion = "1.4.2").isEmpty())
    }
}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./gradlew testProdDebugUnitTest --tests '*DeviceHeadersTest*'
```
Expected: FAIL — unresolved reference `deviceHeaders`.

- [ ] **Step 3: Write the header builder**

Create `app/src/main/java/com/benzn/grandtime/net/DeviceHeaders.kt`:

```kotlin
package com.benzn.grandtime.net

/**
 * The device ledger's heartbeat rides these headers on every org-api request.
 *
 * Deliberately NOT an OkHttp interceptor: RecordingsApiClient derives its S3 upload client
 * from the same builder, so an interceptor would stamp presigned PUTs to S3 as well.
 * Attaching here — beside Authorization — cannot reach S3 by construction.
 *
 * An absent tag is OMITTED rather than sent empty: the backend turns a uuid-without-tag into
 * an `unclaimed:` row so a human can claim it, and an empty string would defeat that.
 */
fun deviceHeaders(assetTag: String?, uuid: String, appVersion: String): List<Pair<String, String>> {
    if (uuid.isBlank()) return emptyList()
    return buildList {
        assetTag?.takeIf { it.isNotBlank() }?.let { add("X-Device-Tag" to it) }
        add("X-Device-Id" to uuid)
        if (appVersion.isNotBlank()) add("X-App-Version" to appVersion)
    }
}
```

- [ ] **Step 4: Attach them in the two request builders**

In `RealHttp.postJson` (`RecordingsApiClient.kt`), after `.header("Authorization", authToken)`:

```kotlin
        val req = Request.Builder().url(url)
            .header("Authorization", authToken)
            .header("Content-Type", "application/json")
            .apply {
                deviceHeaders(
                    DeviceIdentity.assetTag(),
                    DeviceIdentity.deviceUuid(),
                    BuildConfig.VERSION_NAME,
                ).forEach { (k, v) -> header(k, v) }
            }
            .post(jsonBody.toRequestBody("application/json".toMediaType()))
            .build()
```

Do the same in `RealSitesHttp.getJson` (`SitesApiClient.kt`). **Leave `putFile` untouched** — that is the S3 path.

Add the imports (`com.benzn.grandtime.device.DeviceIdentity`, `com.benzn.grandtime.BuildConfig`).

- [ ] **Step 5: Run the full unit suite**

```bash
./gradlew testProdDebugUnitTest
```
Expected: PASS.

- [ ] **Step 6: Prove the S3 path is clean**

```bash
grep -n "deviceHeaders" app/src/main/java/com/benzn/grandtime/net/*.kt
```
Expected: matches in `RealHttp.postJson` and `RealSitesHttp.getJson` only. **A match anywhere near `putFile` or `UPLOAD_HTTP` is a bug** — that is the S3 leak this task exists to avoid.

- [ ] **Step 7: Commit**

```bash
git add app/src/main/java/com/benzn/grandtime/net/DeviceHeaders.kt \
        app/src/main/java/com/benzn/grandtime/net/RecordingsApiClient.kt \
        app/src/main/java/com/benzn/grandtime/net/SitesApiClient.kt \
        app/src/test/java/com/benzn/grandtime/net/DeviceHeadersTest.kt
git commit -m "feat(device): report identity on org-api calls, never on S3 uploads"
```

---

## Task 3: Settings — type the label, read the short code

**Files:**
- Modify: `app/src/main/java/com/benzn/grandtime/ui/SettingsScreen.kt`
- Test: manual, on hardware (Task 5). This is UI wiring over Task 1's already-tested rules.

- [ ] **Step 1: Read the screen's existing conventions**

```bash
grep -n "fun SettingsScreen\|Composable\|SettingsStore" app/src/main/java/com/benzn/grandtime/ui/SettingsScreen.kt | head -20
```
Match whatever row/section pattern the file already uses. Do not introduce a second styling approach.

- [ ] **Step 2: Add a "Device" section**

Two rows:

- **Device number** — an editable text field, prefilled from `DeviceIdentity.assetTag()`, saving via `DeviceIdentity.setAssetTag(...)` on change. Placeholder `FS-01`. Keep it visually plain; this is typed once in the life of the device.
- **Device code** — read-only, `DeviceIdentity.shortCode()`, with a one-line explanation: this is what an administrator matches against an unclaimed row in the ledger.

Both rows are informational for the field user, so put them **below** the capture settings, not above.

- [ ] **Step 3: Verify it compiles and the suite still passes**

```bash
./gradlew testProdDebugUnitTest
./gradlew assembleProdDebug
```
Expected: both succeed. If the Gradle build fails with a file-lock error, that is Dropbox — pause sync and retry.

- [ ] **Step 4: Commit**

```bash
git add app/src/main/java/com/benzn/grandtime/ui/SettingsScreen.kt
git commit -m "feat(device): type the label, read the code, in Settings"
```

---

## Task 4: Warn on logout, do not block

**Files:**
- Modify: wherever logout is invoked (start from `auth/CognitoAuthManager.kt` and follow the caller into the UI)
- Test: `app/src/test/java/com/benzn/grandtime/db/UploadQueueOwnershipTest.kt` (extend)

- [ ] **Step 1: Write the failing test**

```kotlin
    @Test
    fun `the logout warning counts only the leaving account's own pending work`() {
        val rows = listOf(
            rec("a1", "sub-A"), rec("a2", "sub-A", status = "failed"),
            rec("b1", "sub-B"), rec("done", "sub-A", status = "uploaded"),
        )
        val leaving = "sub-A"
        val n = rows.count {
            it.authorSub == leaving && it.uploadStatus in setOf("pending", "failed")
        }
        assertEquals(2, n)
    }
```

- [ ] **Step 2: Run it**

```bash
./gradlew testProdDebugUnitTest --tests '*UploadQueueOwnershipTest*'
```
Expected: PASS immediately — it is a pure assertion about the rule. Its job is to pin the rule so a later edit cannot widen it to "all pending".

- [ ] **Step 3: Show the warning**

Before clearing the session, count the leaving account's pending + failed rows. If greater than zero, show a dialog:

> **N recordings not yet uploaded.** They stay on this device and will upload next time this account signs in with a connection. Sign out anyway?

Buttons: **Sign out** / **Cancel**. **Sign out must proceed.** A hand-over routinely happens on a site with no signal, and blocking would strand the person doing it — that is the whole reason this is a warning and not a guard.

- [ ] **Step 4: Run the suite and commit**

```bash
./gradlew testProdDebugUnitTest
git add -u && git commit -m "feat(auth): say what is unsent on sign-out, do not block it"
```

---

## Task 5: Verify on real hardware

Unit tests cannot see the two things most likely to be wrong: whether the ROM's `ANDROID_ID` is shared across the fleet, and whether the identity really survives "Clear data".

- [ ] **Step 1: Install the prod flavour**

```bash
./gradlew assembleProdDebug
adb install -r app/build/outputs/apk/prod/debug/app-prod-debug.apk
```
Not `.dev` — that flavour talks to the test gateway and its uploads will not appear where you look.

- [ ] **Step 2: Type a label and confirm the heartbeat lands**

Settings → Device number → `FS-01`. Sign in. Then, from the workstation:

```bash
export MSYS_NO_PATHCONV=1
aws lambda invoke --function-name fieldsight-prod-device-ledger \
  --payload '{}' --cli-binary-format raw-in-base64-out \
  --region ap-southeast-2 out.json && cat out.json
```
Expected: a row with `asset_tag: "FS-01"`, a real `app_version`, and a `last_seen_at` from the last few minutes.

(Phase 1 is deployed on TEST only. If prod has not had it yet, point the device at the test gateway or deploy Phase 1 to prod first — do not skip this step, it is the only end-to-end proof.)

- [ ] **Step 3: Check the fleet for a shared ANDROID_ID**

Repeat Step 2 on a **second** device with tag `FS-02`. Then:

```bash
aws lambda invoke --function-name fieldsight-prod-device-ledger ... && cat out.json
```
Compare the two `device_uuid` values. **If they are identical, that is the ROM sharing one ANDROID_ID across the fleet** — the exact scenario `asset_tag`-as-authority was designed for. Confirm `uuid_trusted` has gone `false` on both rows; the ledger stays correct either way. Record the finding in the spec.

- [ ] **Step 4: Confirm the identity survives Clear data**

Settings → Apps → FieldSight → Storage → **Clear data**. Reopen the app. `DeviceIdentity.assetTag()` must still return `FS-01`, read back from the external-files sidecar. If it is empty, the double write is not working and Task 1 Step 5 needs revisiting.

- [ ] **Step 5: Rehearse a hand-over**

With at least one recording still pending: sign out (confirm the warning names the right count), sign in as a different account, and check that the previous account's pending rows are **not** uploaded and **not** listed. Then sign back in as the first account and confirm they resume.

This is the cross-tenant leak from Task 0 — verify it on hardware, not only in tests.

- [ ] **Step 6: Open the PR**

```bash
git push -u origin feat/device-identity-phase2
gh pr create --base main --title "feat(device): report device identity, and stop the queue crossing accounts" --body "..."
```
Include in the body what Steps 3–5 actually showed, especially whether the fleet shares an ANDROID_ID.

---

## Not in this plan

The backend half is already live on TEST (Phase 1, `fieldsight-pipeline` #220). Alert derivation and the Notion sync are Phase 3, a separate plan in the same directory. Nothing here depends on Phase 3, and Phase 3 becomes useful the moment this ships.
