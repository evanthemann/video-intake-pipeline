/* ─────────────────────────────────────────────────────────────────────
   import_blender.jsx — Import a Blender VSE timeline into After Effects

   Pairs with after-effects/vse_export.py.  Run in After Effects:
     File > Scripts > Run Script File…  →  select this file
     (or drop the .jsx into AE's Scripts/ folder to make it available
     under File > Scripts directly)

   Prompts for the *_vse_export.json written by vse_export.py, then
   builds a comp called "Blender_VSE" with each MOVIE strip placed as
   a layer at its timeline start, with the correct in/out points,
   scale, and position.

   Channel sort: lower Blender channels land lower in the AE layer
   stack (bottom-to-top), matching VSE's vertical lane convention.
   ───────────────────────────────────────────────────────────────────── */

/* ---- tiny JSON.parse polyfill for older AE / ExtendScript ---- */
if (typeof JSON !== 'object') { JSON = {}; }
(function () {
    if (typeof JSON.parse !== 'function') {
        JSON.parse = function (text) {
            text = String(text);
            var cx = /[\x00-\x1f\x7f-\x9f]/g;
            if (cx.test(text)) { text = text.replace(cx, ''); }
            return eval('(' + text + ')');
        };
    }
}());

/* ---- pick the JSON ---- */
var jsonFile = File.openDialog("Select the VSE export JSON", "*.json");
if (!jsonFile) {
    alert("✖ No file selected. Exiting.");
    throw new Error("User cancelled");
}

/* ---- read the JSON ---- */
var f = File(jsonFile.fsName);
if (!f.exists) {
    alert("JSON file not found:\n" + jsonFile.fsName);
    throw new Error("JSON not found");
}
f.open("r");
var jsonStr = f.read();
f.close();

var data = JSON.parse(jsonStr);

/* ---- pull comp config out of the JSON ---- */
var fps          = data.fps          || 30;
var compW        = data.comp_width   || 1920;
var compH        = data.comp_height  || 1080;
var compDuration = data.comp_duration|| 60.0;
var clips        = data.clips        || [];
var compName     = "Blender_VSE";

if (clips.length === 0) {
    alert("No clips found in JSON.");
    throw new Error("No clips.");
}

/* ---- create comp ---- */
var proj = app.project || app.newProject();
var comp = proj.items.addComp(compName, compW, compH, 1.0, compDuration, fps);

/* ---- sort: lower channel first (bottom of AE stack), earlier first ---- */
clips.sort(function (a, b) {
    if (a.channel !== b.channel) { return a.channel - b.channel; }
    return a.timeline_start - b.timeline_start;
});

/* ---- import + place every clip ---- */
app.beginUndoGroup("Import Blender Timeline");

var imported = 0;
var missing  = [];

for (var i = 0; i < clips.length; i++) {
    var c       = clips[i];
    var srcFile = File(c.filepath);

    if (!srcFile.exists) {
        missing.push(c.filepath);
        continue;
    }

    var footage = proj.importFile(new ImportOptions(srcFile));
    var layer   = comp.layers.add(footage);

    var timelineStart = c.timeline_start;
    var inPoint       = c.in_point;
    var outPoint      = c.out_point;
    var duration      = outPoint - inPoint;

    /* scale (Blender is unit-normalized, AE is percent) */
    var scaleX = (c.scale_x || 1.0) * 100;
    var scaleY = (c.scale_y || 1.0) * 100;
    layer.property("Scale").setValue([scaleX, scaleY]);

    /* position — Blender VSE Y is up, AE Y is down */
    var pos = layer.property("Position").value;
    pos[0] += (c.translate_x || 0.0);
    pos[1] -= (c.translate_y || 0.0);
    layer.property("Position").setValue(pos);

    layer.startTime = timelineStart - inPoint;
    layer.inPoint   = timelineStart;
    layer.outPoint  = timelineStart + duration;

    imported++;
}

app.endUndoGroup();

var msg = "✓ Imported " + imported + " clip(s) into comp: " + compName;
if (missing.length > 0) {
    msg += "\n\n⚠ Missing media (" + missing.length + "):\n  " + missing.join("\n  ");
}
alert(msg);
