from django import template

register = template.Library()

@register.simple_tag
def get_score(score_dict, loc_id, crit_id):
    """Mengambil skor dari dictionary bertipe {(loc_id, crit_id): score}"""
    if not score_dict:
        return ''
    return score_dict.get((loc_id, crit_id), '')

@register.filter
def get_item(dictionary, key):
    """Mengambil item dari dictionary berdasarkan key string."""
    if not dictionary:
        return {}
    return dictionary.get(key, {})

@register.filter
def is_score_selected(score_dict, loc_and_crit_and_val):
    """Helper untuk memeriksa apakah opsi tertentu terpilih"""
    # Dipakai jika diperlukan
    return False
