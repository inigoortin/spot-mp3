# R8 rules for the release build. The libraries (OkHttp, Coil, WorkManager, Compose) ship their own.
# The app parses JSON by hand with org.json and uses no reflection, so nothing else needs keeping.

# Readable stack traces in crash reports
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
