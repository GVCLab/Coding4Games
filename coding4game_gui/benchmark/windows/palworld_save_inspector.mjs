// Read-only adapter around palworld-save-toolkit. Prints one JSON document.
import { readFileSync } from "fs";
import { join } from "path";
import { pathToFileURL } from "url";

const [toolkitRoot, levelPath, playerPath, ...selectors] = process.argv.slice(2);
const inventoryPrefix = "--inventory-item=";
const inventoryItemIds = selectors
  .filter((item) => item.startsWith(inventoryPrefix))
  .map((item) => item.slice(inventoryPrefix.length));
const targetCharacterIds = selectors.filter(
  (item) => !item.startsWith(inventoryPrefix),
);
if (!toolkitRoot || !levelPath || !playerPath || targetCharacterIds.length === 0) {
  throw new Error("usage: palworld_save_inspector.mjs TOOLKIT LEVEL PLAYER TARGET_ID...");
}
const targetIds = new Set(targetCharacterIds);

const migrate = await import(pathToFileURL(join(toolkitRoot, "docs/js/migrate.js")));
const oozModule = await import(
  pathToFileURL(join(toolkitRoot, "docs/vendor/ooz-wasm/index.js"))
);
const ooz = oozModule.decompress;
const levelBytes = new Uint8Array(readFileSync(levelPath));
const playerBytes = new Uint8Array(readFileSync(playerPath));
const [summary, parsedLevel, parsedPlayer] = await Promise.all([
  migrate.inspectWorld(levelBytes, ooz),
  migrate.parseLevel(levelBytes, ooz),
  migrate.parsePlayer(playerBytes, ooz),
]);

const saveData = parsedPlayer.player.properties.SaveData.value;
const playerUid = String(saveData.PlayerUId.value);
const palboxId = String(saveData.PalStorageContainerId.value.ID.value);
const world = parsedLevel.level.properties.worldSaveData.value;
const commonContainerId = String(
  saveData.InventoryInfo.value.CommonContainerId.value.ID.value,
);
const commonContainer = world.ItemContainerSaveData.value.find(
  (entry) => String(entry.key?.ID?.value ?? "") === commonContainerId,
);
if (!commonContainer) {
  throw new Error(`Common inventory container not found: ${commonContainerId}`);
}

function uint32le(bytes, offset) {
  return ((bytes[offset] ?? 0)
    | ((bytes[offset + 1] ?? 0) << 8)
    | ((bytes[offset + 2] ?? 0) << 16)
    | ((bytes[offset + 3] ?? 0) << 24)) >>> 0;
}

function decodeInventorySlot(slot) {
  const raw = slot.RawData?.value?.values;
  if (!raw) return null;
  const bytes = Uint8Array.from(Object.values(raw));
  if (bytes.length < 13) return null;
  const quantity = uint32le(bytes, 4);
  const encodedLength = uint32le(bytes, 8);
  if (encodedLength < 1 || 12 + encodedLength > bytes.length) return null;
  const itemId = new TextDecoder("utf-8")
    .decode(bytes.slice(12, 12 + encodedLength - 1));
  return itemId ? { item_id: itemId, quantity } : null;
}

const requestedInventoryIds = new Set(inventoryItemIds);
const inventoryItems = {};
for (const slot of commonContainer.value.Slots?.value?.values ?? []) {
  const decoded = decodeInventorySlot(slot);
  if (!decoded || !requestedInventoryIds.has(decoded.item_id)) continue;
  inventoryItems[decoded.item_id] =
    (inventoryItems[decoded.item_id] ?? 0) + decoded.quantity;
}
const palbox = world.CharacterContainerSaveData.value.find(
  (entry) => String(entry.key?.ID?.value ?? "") === palboxId,
);
if (!palbox) throw new Error(`Palbox container not found: ${palboxId}`);
const palboxInstances = new Set(
  (palbox.value.Slots?.value?.values ?? [])
    .map((slot) => slot.RawData?.value?.instance_id)
    .filter(Boolean)
    .map(String),
);
const targetInstances = summary.pals
  .filter((pal) => pal.owner === playerUid && targetIds.has(pal.characterId))
  .map((pal) => ({
    instance_id: pal.instanceId,
    owner_uid: pal.owner,
    character_id: pal.characterId,
    species: pal.species,
    alpha: pal.alpha,
    level: pal.level,
    in_palbox: palboxInstances.has(pal.instanceId),
  }));

process.stdout.write(JSON.stringify({
  player_uid: playerUid,
  palbox_id: palboxId,
  owned_count: summary.pals.filter((pal) => pal.owner === playerUid).length,
  inventory_items: inventoryItems,
  target_instances: targetInstances,
}));
