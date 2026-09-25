#ifndef __LCD_MENU_H__
#define __LCD_MENU_H__

#include "menu_func.h"
#include "communication.h"
#include "monitor.h"

void refresh_menu();
void pre_menu_init();

extern MENU menu;
extern USB_Comm comm;

#endif
