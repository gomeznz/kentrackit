#!/usr/bin/env python3
"""Writes KenTrackIt.xcodeproj/project.pbxproj. Run after adding or removing a source file:
    python3 ios/make_project.py
(Xcode can also add files itself; this keeps the project readable and the ids stable.)"""
import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = ["AppDelegate.swift", "SceneDelegate.swift", "Core.swift", "Alerts.swift", "Push.swift", "LocationSharing.swift",
           "WebViewController.swift", "SetupViewController.swift", "SettingsViewController.swift"]
RESOURCES = [("Assets.xcassets", "folder.assetcatalog"), ("alarm.wav", "audio.wav")]
BUNDLE_ID = "nz.co.asiltd.kentrackit"
MARKETING_VERSION = "1.0"
BUILD = "3"
# ASI's Apple Team ID (Membership page on developer.apple.com); empty = choose a team in Xcode
TEAM_ID = ""


def oid(name):
    return hashlib.md5(name.encode()).hexdigest()[:24].upper()


def main():
    P, MAIN, APPG, PRODG, APP = oid("project"), oid("maingroup"), oid("appgroup"), oid("products"), oid("app.product")
    T, SRC, RES, FW = oid("target"), oid("sources.phase"), oid("resources.phase"), oid("frameworks.phase")
    PCL, TCL = oid("project.configlist"), oid("target.configlist")
    PD, PR, TD, TR = oid("proj.debug"), oid("proj.release"), oid("tgt.debug"), oid("tgt.release")
    PLIST = oid("ref.Info.plist")
    ENT = oid("ref.KenTrackIt.entitlements")
    o = []
    w = o.append
    w("// !$*UTF8*$!\n{\n\tarchiveVersion = 1;\n\tclasses = {\n\t};\n\tobjectVersion = 56;\n\tobjects = {\n")
    w("\n/* Begin PBXBuildFile section */\n")
    for f in SOURCES:
        w(f"\t\t{oid('build.' + f)} /* {f} in Sources */ = {{isa = PBXBuildFile; fileRef = {oid('ref.' + f)} /* {f} */; }};\n")
    for f, _ in RESOURCES:
        w(f"\t\t{oid('build.' + f)} /* {f} in Resources */ = {{isa = PBXBuildFile; fileRef = {oid('ref.' + f)} /* {f} */; }};\n")
    w("/* End PBXBuildFile section */\n\n/* Begin PBXFileReference section */\n")
    w(f"\t\t{APP} /* KenTrackIt.app */ = {{isa = PBXFileReference; explicitFileType = wrapper.application; "
      f"includeInIndex = 0; path = KenTrackIt.app; sourceTree = BUILT_PRODUCTS_DIR; }};\n")
    for f in SOURCES:
        w(f"\t\t{oid('ref.' + f)} /* {f} */ = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {f}; sourceTree = \"<group>\"; }};\n")
    for f, t in RESOURCES:
        w(f"\t\t{oid('ref.' + f)} /* {f} */ = {{isa = PBXFileReference; lastKnownFileType = {t}; path = {f}; sourceTree = \"<group>\"; }};\n")
    w(f"\t\t{PLIST} /* Info.plist */ = {{isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = \"<group>\"; }};\n")
    w(f"\t\t{ENT} /* KenTrackIt.entitlements */ = {{isa = PBXFileReference; lastKnownFileType = text.plist.entitlements; path = KenTrackIt.entitlements; sourceTree = \"<group>\"; }};\n")
    w("/* End PBXFileReference section */\n\n/* Begin PBXFrameworksBuildPhase section */\n")
    w(f"\t\t{FW} /* Frameworks */ = {{\n\t\t\tisa = PBXFrameworksBuildPhase;\n\t\t\tbuildActionMask = 2147483647;\n"
      f"\t\t\tfiles = (\n\t\t\t);\n\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n")
    w("/* End PBXFrameworksBuildPhase section */\n\n/* Begin PBXGroup section */\n")
    w(f"\t\t{MAIN} = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n\t\t\t\t{APPG} /* KenTrackIt */,\n"
      f"\t\t\t\t{PRODG} /* Products */,\n\t\t\t);\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")
    kids = "".join(f"\t\t\t\t{oid('ref.' + f)} /* {f} */,\n" for f in SOURCES)
    kids += "".join(f"\t\t\t\t{oid('ref.' + f)} /* {f} */,\n" for f, _ in RESOURCES)
    kids += f"\t\t\t\t{PLIST} /* Info.plist */,\n"
    kids += f"\t\t\t\t{ENT} /* KenTrackIt.entitlements */,\n"
    w(f"\t\t{APPG} /* KenTrackIt */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n{kids}\t\t\t);\n"
      f"\t\t\tpath = KenTrackIt;\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")
    w(f"\t\t{PRODG} /* Products */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n\t\t\t\t{APP} /* KenTrackIt.app */,\n"
      f"\t\t\t);\n\t\t\tname = Products;\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")
    w("/* End PBXGroup section */\n\n/* Begin PBXNativeTarget section */\n")
    w(f"\t\t{T} /* KenTrackIt */ = {{\n\t\t\tisa = PBXNativeTarget;\n"
      f"\t\t\tbuildConfigurationList = {TCL} /* Build configuration list for PBXNativeTarget \"KenTrackIt\" */;\n"
      f"\t\t\tbuildPhases = (\n\t\t\t\t{SRC} /* Sources */,\n\t\t\t\t{FW} /* Frameworks */,\n\t\t\t\t{RES} /* Resources */,\n\t\t\t);\n"
      f"\t\t\tbuildRules = (\n\t\t\t);\n\t\t\tdependencies = (\n\t\t\t);\n\t\t\tname = KenTrackIt;\n\t\t\tproductName = KenTrackIt;\n"
      f"\t\t\tproductReference = {APP} /* KenTrackIt.app */;\n\t\t\tproductType = \"com.apple.product-type.application\";\n\t\t}};\n")
    w("/* End PBXNativeTarget section */\n\n/* Begin PBXProject section */\n")
    w(f"\t\t{P} /* Project object */ = {{\n\t\t\tisa = PBXProject;\n\t\t\tattributes = {{\n"
      f"\t\t\t\tBuildIndependentTargetsInParallel = 1;\n\t\t\t\tLastSwiftUpdateCheck = 1500;\n\t\t\t\tLastUpgradeCheck = 1500;\n"
      f"\t\t\t\tTargetAttributes = {{\n\t\t\t\t\t{T} = {{\n\t\t\t\t\t\tCreatedOnToolsVersion = 15.0;\n\t\t\t\t\t}};\n\t\t\t\t}};\n\t\t\t}};\n"
      f"\t\t\tbuildConfigurationList = {PCL} /* Build configuration list for PBXProject \"KenTrackIt\" */;\n"
      f"\t\t\tcompatibilityVersion = \"Xcode 14.0\";\n\t\t\tdevelopmentRegion = en;\n\t\t\thasScannedForEncodings = 0;\n"
      f"\t\t\tknownRegions = (\n\t\t\t\ten,\n\t\t\t\tBase,\n\t\t\t);\n\t\t\tmainGroup = {MAIN};\n"
      f"\t\t\tproductRefGroup = {PRODG} /* Products */;\n\t\t\tprojectDirPath = \"\";\n\t\t\tprojectRoot = \"\";\n"
      f"\t\t\ttargets = (\n\t\t\t\t{T} /* KenTrackIt */,\n\t\t\t);\n\t\t}};\n")
    w("/* End PBXProject section */\n\n/* Begin PBXResourcesBuildPhase section */\n")
    rf = "".join(f"\t\t\t\t{oid('build.' + f)} /* {f} in Resources */,\n" for f, _ in RESOURCES)
    w(f"\t\t{RES} /* Resources */ = {{\n\t\t\tisa = PBXResourcesBuildPhase;\n\t\t\tbuildActionMask = 2147483647;\n"
      f"\t\t\tfiles = (\n{rf}\t\t\t);\n\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n")
    w("/* End PBXResourcesBuildPhase section */\n\n/* Begin PBXSourcesBuildPhase section */\n")
    sf = "".join(f"\t\t\t\t{oid('build.' + f)} /* {f} in Sources */,\n" for f in SOURCES)
    w(f"\t\t{SRC} /* Sources */ = {{\n\t\t\tisa = PBXSourcesBuildPhase;\n\t\t\tbuildActionMask = 2147483647;\n"
      f"\t\t\tfiles = (\n{sf}\t\t\t);\n\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n")
    w("/* End PBXSourcesBuildPhase section */\n\n/* Begin XCBuildConfiguration section */\n")
    common = {"ALWAYS_SEARCH_USER_PATHS": "NO", "CLANG_ENABLE_MODULES": "YES", "CLANG_ENABLE_OBJC_ARC": "YES",
              "COPY_PHASE_STRIP": "NO", "ENABLE_STRICT_OBJC_MSGSEND": "YES", "GCC_C_LANGUAGE_STANDARD": "gnu17",
              "IPHONEOS_DEPLOYMENT_TARGET": "16.0", "SDKROOT": "iphoneos", "SWIFT_VERSION": "5.0"}
    pdebug = dict(common, DEBUG_INFORMATION_FORMAT="dwarf", ENABLE_TESTABILITY="YES", GCC_OPTIMIZATION_LEVEL="0",
                  ONLY_ACTIVE_ARCH="YES", SWIFT_ACTIVE_COMPILATION_CONDITIONS='"DEBUG $(inherited)"',
                  SWIFT_OPTIMIZATION_LEVEL='"-Onone"', MTL_ENABLE_DEBUG_INFO="INCLUDE_SOURCE")
    prelease = dict(common, DEBUG_INFORMATION_FORMAT='"dwarf-with-dsym"', ENABLE_NS_ASSERTIONS="NO",
                    SWIFT_COMPILATION_MODE="wholemodule", VALIDATE_PRODUCT="YES", MTL_ENABLE_DEBUG_INFO="NO")
    tgt = {"ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon", "CODE_SIGN_STYLE": "Automatic",
           "CURRENT_PROJECT_VERSION": BUILD, "DEVELOPMENT_TEAM": TEAM_ID or '""',
           "CODE_SIGN_ENTITLEMENTS": "KenTrackIt/KenTrackIt.entitlements", "ENABLE_PREVIEWS": "NO",
           "GENERATE_INFOPLIST_FILE": "NO", "INFOPLIST_FILE": "KenTrackIt/Info.plist",
           "LD_RUNPATH_SEARCH_PATHS": '(\n\t\t\t\t\t"$(inherited)",\n\t\t\t\t\t"@executable_path/Frameworks",\n\t\t\t\t)',
           "MARKETING_VERSION": MARKETING_VERSION, "PRODUCT_BUNDLE_IDENTIFIER": BUNDLE_ID,
           "PRODUCT_NAME": '"$(TARGET_NAME)"', "SWIFT_EMIT_LOC_STRINGS": "NO", "SWIFT_VERSION": "5.0",
           "TARGETED_DEVICE_FAMILY": "1"}
    for i, name, d in [(PD, "Debug", pdebug), (PR, "Release", prelease), (TD, "Debug", tgt), (TR, "Release", tgt)]:
        body = "".join(f"\t\t\t\t{k} = {v};\n" for k, v in d.items())
        w(f"\t\t{i} /* {name} */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n{body}\t\t\t}};\n"
          f"\t\t\tname = {name};\n\t\t}};\n")
    w("/* End XCBuildConfiguration section */\n\n/* Begin XCConfigurationList section */\n")
    for cl, what, d, r in [(PCL, 'PBXProject "KenTrackIt"', PD, PR), (TCL, 'PBXNativeTarget "KenTrackIt"', TD, TR)]:
        w(f"\t\t{cl} /* Build configuration list for {what} */ = {{\n\t\t\tisa = XCConfigurationList;\n"
          f"\t\t\tbuildConfigurations = (\n\t\t\t\t{d} /* Debug */,\n\t\t\t\t{r} /* Release */,\n\t\t\t);\n"
          f"\t\t\tdefaultConfigurationIsVisible = 0;\n\t\t\tdefaultConfigurationName = Release;\n\t\t}};\n")
    w("/* End XCConfigurationList section */\n\t};\n\trootObject = " + P + " /* Project object */;\n}\n")
    with open(os.path.join(HERE, "KenTrackIt.xcodeproj", "project.pbxproj"), "w") as f:
        f.write("".join(o))


if __name__ == "__main__":
    main()
