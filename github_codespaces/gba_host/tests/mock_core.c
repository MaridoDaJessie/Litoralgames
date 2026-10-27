#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
struct game_info{const char*path;const void*data;size_t size;const char*meta;};
struct geom{unsigned w,h,mw,mh;float ratio;};
struct timing{double fps,sample;};
struct av{struct geom g;struct timing t;};
static bool (*env)(unsigned,void*);static void (*video)(const void*,unsigned,unsigned,size_t);
static void (*audio)(int16_t,int16_t);static size_t (*batch)(const int16_t*,size_t);
static void (*poll)(void);static int16_t (*input)(unsigned,unsigned,unsigned,unsigned);
static uint16_t buf[240*160];static int ctr;
void retro_set_environment(bool (*f)(unsigned,void*)){env=f;}
void retro_set_video_refresh(void (*f)(const void*,unsigned,unsigned,size_t)){video=f;}
void retro_set_audio_sample(void (*f)(int16_t,int16_t)){audio=f;}
void retro_set_audio_sample_batch(size_t (*f)(const int16_t*,size_t)){batch=f;}
void retro_set_input_poll(void (*f)(void)){poll=f;}
void retro_set_input_state(int16_t (*f)(unsigned,unsigned,unsigned,unsigned)){input=f;}
unsigned retro_api_version(void){return 1;}
void retro_init(void){int fmt=2;env(10,&fmt);}
void retro_deinit(void){}
bool retro_load_game(const struct game_info *g){return (g && g->size>=192);}
void retro_unload_game(void){}
void retro_reset(void){ctr=0;}
void retro_get_system_av_info(struct av* a){a->g=(struct geom){240,160,240,160,1.5f};a->t=(struct timing){59.7275,32768};}
void retro_run(void){int pressed=input(0,1,0,8);for(int y=0;y<160;y++)for(int x=0;x<240;x++)buf[y*240+x]=(((ctr/3+x)%32)<<11)|(((y/2)%64)<<5)|(pressed?31:((x/16)%32));ctr++; video(buf,240,160,240*2);}
