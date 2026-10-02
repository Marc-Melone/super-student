// Small "getting ready" window shown while the Mac app sets itself up the first time.
// Run by the launcher: osascript -l JavaScript progress.js <status file> <icon.png> <first|update>
// It shows the line in the status file and closes when the file says "done" or "failed".
ObjC.import("Cocoa");

function run(argv) {
  var statusFile = argv[0], iconPath = argv[1], mode = argv[2] || "first";
  var app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(1); // accessory: a window, no extra Dock icon
  var W = 460, H = 150;
  var win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer($.NSMakeRect(0, 0, W, H), 1, 2, false);
  win.title = "Super Student";
  win.releasedWhenClosed = false;
  var view = win.contentView;

  var icon = $.NSImageView.alloc.initWithFrame($.NSMakeRect(22, H - 90, 64, 64));
  var image = $.NSImage.alloc.initWithContentsOfFile(iconPath);
  if (image && !image.isNil()) icon.image = image;
  view.addSubview(icon);

  var title = $.NSTextField.labelWithString(mode === "update" ? "Updating Super Student" : "Getting Super Student ready");
  title.font = $.NSFont.boldSystemFontOfSize(14);
  title.frame = $.NSMakeRect(104, H - 46, W - 126, 20);
  view.addSubview(title);

  var label = $.NSTextField.wrappingLabelWithString("Starting");
  label.frame = $.NSMakeRect(104, H - 96, W - 126, 44);
  view.addSubview(label);

  var bar = $.NSProgressIndicator.alloc.initWithFrame($.NSMakeRect(104, 24, W - 126, 20));
  bar.indeterminate = true;
  bar.usesThreadedAnimation = true;
  view.addSubview(bar);
  bar.startAnimation(null);

  win.center;
  win.makeKeyAndOrderFront(null);
  app.activateIgnoringOtherApps(true);

  var last = "";
  for (var i = 0; i < 20000; i++) {   // about 80 minutes at most
    $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.25));
    var s = $.NSString.stringWithContentsOfFileEncodingError(statusFile, $.NSUTF8StringEncoding, null);
    var text = (s && !s.isNil()) ? ObjC.unwrap(s).trim() : "";
    if (text === "done" || text === "failed") break;
    if (text && text !== last) {
      label.stringValue = text + "…";
      last = text;
    }
  }
  bar.stopAnimation(null);
  win.orderOut(null);
}
