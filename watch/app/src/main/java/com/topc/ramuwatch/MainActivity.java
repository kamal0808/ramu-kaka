package com.topc.ramuwatch;

import android.Manifest;
import android.app.Activity;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.net.ConnectivityManager;
import android.net.wifi.WifiManager;
import android.os.PowerManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import java.net.Inet4Address;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.TextView;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;

// Tap to stream the watch mic to the Mac (16 kHz mono PCM16 over TCP), tap again to stop.
public class MainActivity extends Activity {
    static final int PORT = 5577;
    static final int BEACON = 5578;  // Mac answers here so finding it never starts a dictation
    static final int RATE = 16000;


    TextView button;
    volatile boolean streaming;
    Network wifi;
    ConnectivityManager cm;
    ConnectivityManager.NetworkCallback wifiCallback;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        SharedPreferences prefs = getSharedPreferences("ramu", MODE_PRIVATE);
        String fromIntent = getIntent().getStringExtra("host");
        if (fromIntent != null) prefs.edit().putString("host", fromIntent).apply();

        button = new TextView(this);
        button.setGravity(Gravity.CENTER);
        button.setTextSize(22);
        button.setTextColor(Color.WHITE);
        button.setOnClickListener(v -> toggle());
        setContentView(button);
        show("Tap to talk", Color.rgb(40, 40, 40));

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
        }
        // Wear OS routes traffic over the phone's Bluetooth by default; ask for Wi-Fi so the Mac on the LAN is reachable.
        cm = getSystemService(ConnectivityManager.class);
        wifiCallback = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network n) { wifi = n; findMac(n); }
            @Override public void onLost(Network n) { if (n.equals(wifi)) wifi = null; }
        };
        cm.requestNetwork(new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build(), wifiCallback);
        startListening();
    }

    // Opening the app (e.g. double-press Home) starts talking straight away; a tap stops.
    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        startListening();
    }

    void startListening() {
        if (!streaming && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) toggle();
    }

    NsdManager nsd;
    NsdManager.DiscoveryListener discovery;

    // The Mac announces "_ramumic._tcp" on the LAN; remember whichever Mac answers so any shared Wi-Fi works.
    void findMac(Network n) {
        if (discovery != null) return;
        Log.i("RamuWatch", "discovery on " + (n == null ? "default network" : "wifi " + n));
        nsd = getSystemService(NsdManager.class);
        discovery = new NsdManager.DiscoveryListener() {
            @Override public void onServiceFound(NsdServiceInfo info) {
                nsd.resolveService(info, new NsdManager.ResolveListener() {
                    @Override public void onServiceResolved(NsdServiceInfo r) {
                        Log.i("RamuWatch", "resolved " + r.getHost());
                        if (r.getHost() instanceof Inet4Address) {
                            getSharedPreferences("ramu", MODE_PRIVATE).edit().putString("host", r.getHost().getHostAddress()).apply();
                        }
                    }
                    @Override public void onResolveFailed(NsdServiceInfo r, int code) {}
                });
            }
            @Override public void onServiceLost(NsdServiceInfo info) {}
            @Override public void onDiscoveryStarted(String t) {}
            @Override public void onDiscoveryStopped(String t) {}
            @Override public void onStartDiscoveryFailed(String t, int code) { Log.i("RamuWatch", "discovery failed " + code); discovery = null; }
            @Override public void onStopDiscoveryFailed(String t, int code) {}
        };
        if (n != null) nsd.discoverServices("_ramumic._tcp", NsdManager.PROTOCOL_DNS_SD, n, Runnable::run, discovery);
        else nsd.discoverServices("_ramumic._tcp", NsdManager.PROTOCOL_DNS_SD, discovery);
    }

    @Override
    protected void onDestroy() {
        streaming = false;
        if (discovery != null) nsd.stopServiceDiscovery(discovery);
        cm.unregisterNetworkCallback(wifiCallback);
        super.onDestroy();
    }

    void show(String text, int color) {
        runOnUiThread(() -> { button.setText(text); button.setBackgroundColor(color); });
    }

    void toggle() {
        if (streaming) { streaming = false; return; }
        streaming = true;
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        show("Listening…\ntap to stop", Color.rgb(170, 30, 30));
        new Thread(this::stream).start();
    }

    static boolean reachable(String host, int ms) {
        try (Socket t = new Socket()) { t.connect(new InetSocketAddress(host, BEACON), ms); return true; } catch (Exception e) { return false; }
    }

    // Bonjour often never reaches a watch (routers drop multicast), so knock on every address on our /24 for Watch Mic's port.
    String scanForMac() throws Exception {
        String prefix = null, self = null;
        for (java.net.NetworkInterface ni : java.util.Collections.list(java.net.NetworkInterface.getNetworkInterfaces())) {
            if (!ni.isUp() || ni.isLoopback() || !ni.getName().startsWith("wlan")) continue;
            for (java.net.InetAddress a : java.util.Collections.list(ni.getInetAddresses())) {
                if (a instanceof Inet4Address) { self = a.getHostAddress(); prefix = self.substring(0, self.lastIndexOf('.') + 1); }
            }
        }
        Log.i("RamuWatch", "scanning " + prefix + "*");
        if (prefix == null) return null;
        java.util.concurrent.ExecutorService pool = java.util.concurrent.Executors.newFixedThreadPool(64);
        java.util.concurrent.CompletionService<String> done = new java.util.concurrent.ExecutorCompletionService<>(pool);
        final String me = self;
        for (int i = 1; i < 255; i++) { String h = prefix + i; done.submit(() -> !h.equals(me) && reachable(h, 500) ? h : null); }
        String found = null;
        for (int i = 1; i < 255 && found == null; i++) found = done.take().get();
        pool.shutdownNow();
        Log.i("RamuWatch", "found " + found);
        return found;
    }

    @Override
    protected void onResume() {
        super.onResume();
        // Samsung freezes the app in the background, which ends discovery; look again each time it's opened.
        if (discovery != null) { try { nsd.stopServiceDiscovery(discovery); } catch (Exception ignored) {} discovery = null; }
        findMac(wifi);
    }

    void stream() {
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        byte[] buf = new byte[Math.max(min, 3200)];
        AudioRecord rec = null;
        // Wear OS turns Wi-Fi off within seconds to save battery; hold it (and the CPU) while talking.
        WifiManager.WifiLock wifiLock = getSystemService(WifiManager.class).createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "ramu:talk");
        PowerManager.WakeLock cpu = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ramu:talk");
        wifiLock.acquire();
        cpu.acquire(10 * 60 * 1000L);
        try {
            // Record from the tap; a 10 s buffer holds speech while we find the Mac, so the first words aren't lost.
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, RATE * 2 * 10);
            rec.startRecording();
            for (int i = 0; i < 30 && wifi == null; i++) Thread.sleep(100);
            Log.i("RamuWatch", "tap: wifi=" + wifi + " discovery=" + (discovery != null));
            if (discovery == null) findMac(wifi);
            SharedPreferences prefs = getSharedPreferences("ramu", MODE_PRIVATE);
            for (int i = 0; i < 20 && prefs.getString("host", null) == null; i++) Thread.sleep(100);
            String host = prefs.getString("host", null);
            if (host == null || !reachable(host, 400)) {
                host = scanForMac();
                if (host != null) prefs.edit().putString("host", host).apply();
            }
            if (host == null) throw new java.io.IOException("no Mac found on this Wi-Fi");
            Socket s = wifi != null ? wifi.getSocketFactory().createSocket() : new Socket();
            s.setTcpNoDelay(true);
            s.connect(new InetSocketAddress(host, PORT), 3000);
            try (OutputStream out = s.getOutputStream()) {
                while (streaming) {
                    int n = rec.read(buf, 0, buf.length);
                    if (n > 0) out.write(buf, 0, n);
                }
            }
            s.close();
            show("Tap to talk", Color.rgb(40, 40, 40));
        } catch (Exception e) {
            show("Can't reach Mac\n" + e.getClass().getSimpleName(), Color.rgb(120, 60, 0));
        } finally {
            streaming = false;
            wifiLock.release();
            if (cpu.isHeld()) cpu.release();
            if (rec != null) { rec.stop(); rec.release(); }
            runOnUiThread(() -> getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
        }
    }
}
