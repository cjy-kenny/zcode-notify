package com.zcodenotify.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 手机重启后自动恢复通知服务（无需打开 app）。 */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        if (Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            NotifyService.start(context);
        }
    }
}
