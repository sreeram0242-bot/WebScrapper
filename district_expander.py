"""
District & Sub-Area Expansion Engine for Google Maps Scraper.
Automatically expands queries like 'gyms in karur' or 'hotels in coimbatore'
into comprehensive sub-area searches covering every locality in that district.
"""

import re
from typing import List, Tuple, Dict

# Comprehensive District Sub-Area Directory (Tamil Nadu, Karnataka, Major Indian Metros)
DISTRICT_SUB_AREAS: Dict[str, List[str]] = {
    # Tamil Nadu Districts
    "karur": [
        "Karur Central", "Thanthonimalai", "Gandhigramam", "Vengamedu", 
        "Pasupathypalayam", "Rayanur", "Sanapiratti", "Inam Karur", 
        "Puliyur", "Velur Road Karur", "Kovai Road Karur", "Kulithalai", 
        "Aravakurichi", "Pallapatti", "Manmangalam"
    ],
    "chennai": [
        "Anna Nagar", "T. Nagar", "Velachery", "Adyar", "Mylapore", 
        "Guindy", "Porur", "Tambaram", "Alwarpet", "Nungambakkam", 
        "Kilpauk", "Kodambakkam", "Chromepet", "OMR Chennai", "Sholinganallur", 
        "Perambur", "Royapettah", "Ashok Nagar Chennai", "Vadapalani", 
        "Saidapet", "Besant Nagar", "Pallavaram", "Medavakkam", "Thoraipakkam"
    ],
    "coimbatore": [
        "Gandhipuram", "RS Puram", "Peelamedu", "Saibaba Colony", 
        "Saravanampatti", "Singanallur", "Ramanathapuram Coimbatore", 
        "Hopes College", "Ganapathy Coimbatore", "Vadavalli", "Thudiyalur", 
        "Kovaipudur", "Ukkadam", "Kuniamuthur", "Pollachi", "Mettupalayam"
    ],
    "madurai": [
        "KK Nagar Madurai", "Anna Nagar Madurai", "Simmakkal", "Goripalayam", 
        "Mattuthavani", "Villapuram", "Teppakulam", "Pasumalai", "Thirunagar", 
        "Sellur", "Othakadai", "Kochadai", "Melur", "Thirumangalam"
    ],
    "trichy": [
        "Thillai Nagar", "Cantonment Trichy", "Srirangam", "KK Nagar Trichy", 
        "Woraiyur", "Ponmalai", "Kattur Trichy", "BHEL Trichy", "Karumandapam", 
        "Palakkarai", "Lalgudi", "Manapparai"
    ],
    "tiruchirappalli": [
        "Thillai Nagar", "Cantonment Trichy", "Srirangam", "KK Nagar Trichy", 
        "Woraiyur", "Ponmalai", "Kattur Trichy", "BHEL Trichy", "Karumandapam", 
        "Palakkarai", "Lalgudi", "Manapparai"
    ],
    "salem": [
        "Fairlands Salem", "Alagapuram", "Suramangalam", "Hasthampatti", 
        "Shevapet", "Ammapet Salem", "Gugai", "Meyyanur", "Kannankurichi", 
        "Attur", "Mettur"
    ],
    "erode": [
        "Perundurai", "Bhavani", "Kollampalayam", "Brough Road Erode", 
        "Veerappanchatram", "Thindal", "Gobichettipalayam", "Sathyamangalam"
    ],
    "tirupur": [
        "Avinashi Road Tirupur", "Kangeyam Road Tirupur", "Palladam Road", 
        "Dharapuram Road", "Mangalam Road Tirupur", "Uthukuli", "Avinashi", "Palladam"
    ],
    "dindigul": [
        "Dindigul Town", "Palani", "Kodaikanal", "Oddanchatram", 
        "Vedasandur", "Natham", "Batlagundu"
    ],
    "thanjavur": [
        "Thanjavur Town", "Kumbakonam", "Pattukkottai", "Thiruvaiyaru", 
        "Papanasam", "Orathanadu"
    ],
    "tirunelveli": [
        "Palayamkottai", "Tirunelveli Junction", "Tirunelveli Town", 
        "Melapalayam", "Ambasamudram", "Valliyur"
    ],
    "thoothukudi": [
        "Thoothukudi Town", "Kovilpatti", "Tiruchendur", "Millerpuram", 
        "Ettayapuram", "Sathankulam"
    ],
    "tuticorin": [
        "Thoothukudi Town", "Kovilpatti", "Tiruchendur", "Millerpuram", 
        "Ettayapuram", "Sathankulam"
    ],
    "kanyakumari": [
        "Nagercoil", "Marthandam", "Thuckalay", "Kanyakumari Town", 
        "Colachel", "Kallukatti"
    ],
    "nagercoil": [
        "Nagercoil Town", "Marthandam", "Thuckalay", "Kanyakumari", 
        "Colachel", "Vadasery"
    ],
    "vellore": [
        "Katpadi", "Sathuvachari", "Bagayam", "Gudiyatham", 
        "Vellore Town", "Gandhi Nagar Vellore"
    ],
    "cuddalore": [
        "Cuddalore Town", "Chidambaram", "Panruti", "Neyveli", 
        "Vadalur", "Virudhachalam"
    ],
    "kanchipuram": [
        "Kanchipuram Town", "Walajabad", "Sriperumbudur", "Uthiramerur"
    ],
    "chengalpattu": [
        "Chengalpattu Town", "Tambaram", "Maraimalai Nagar", 
        "Guduvanchery", "Singaperumal Koil"
    ],
    "tiruvallur": [
        "Tiruvallur Town", "Avadi", "Ambattur", "Poonamallee", "Gummidipoondi"
    ],
    "hosur": [
        "Hosur Town", "Bagalur", "SIPCOT Hosur", "Zuzuvadi", "Denkanikottai"
    ],
    "krishnagiri": [
        "Krishnagiri Town", "Hosur", "Denkanikottai", "Pochampalli"
    ],
    "dharmapuri": [
        "Dharmapuri Town", "Harur", "Palacode", "Pennagaram"
    ],
    "namakkal": [
        "Namakkal Town", "Rasipuram", "Tiruchengode", "Paramathi Velur", "Komarapalayam"
    ],
    "pudukkottai": [
        "Pudukkottai Town", "Aranthangi", "Alangudi", "Viralimalai"
    ],
    "sivaganga": [
        "Karaikudi", "Sivaganga Town", "Devakottai", "Manamadurai"
    ],
    "ramanathapuram": [
        "Ramanathapuram Town", "Rameswaram", "Paramakudi", "Kilakarai"
    ],
    "theni": [
        "Theni Town", "Periyakulam", "Bodinayakanur", "Cumbum", "Chinnamanur"
    ],
    "virudhunagar": [
        "Sivakasi", "Rajapalayam", "Virudhunagar Town", "Aruppukkottai", "Srivilliputhur"
    ],
    "nilgiris": [
        "Ooty", "Coonoor", "Kotagiri", "Gudalur"
    ],
    "ooty": [
        "Ooty Town", "Coonoor", "Kotagiri", "Lovedale", "Fern Hill"
    ],
    "nagapattinam": [
        "Nagapattinam Town", "Velankanni", "Nagore", "Vedaranyam"
    ],
    "mayiladuthurai": [
        "Mayiladuthurai Town", "Sirkazhi", "Tharangambadi", "Kuthalam"
    ],
    "thiruvarur": [
        "Thiruvarur Town", "Mannargudi", "Thiruthuraipoondi", "Nannilam"
    ],
    "villupuram": [
        "Villupuram Town", "Tindivanam", "Gingee", "Vikravandi"
    ],
    "kallakurichi": [
        "Kallakurichi Town", "Ulundurpet", "Sankarapuram", "Tirukoilur"
    ],
    "tiruvannamalai": [
        "Tiruvannamalai Town", "Polur", "Arani", "Cheyyar", "Vandavasi"
    ],
    "ranipet": [
        "Ranipet Town", "Arcot", "Walaja", "Arakkonam", "Sholinghur"
    ],
    "tirupathur": [
        "Tirupathur Town", "Vaniyambadi", "Ambur", "Jolarpet"
    ],
    "tenkasi": [
        "Tenkasi Town", "Sankarankovil", "Kadayanallur", "Puliyangudi", "Surandai"
    ],

    # Major Indian Metro Hubs
    "bangalore": [
        "Koramangala", "Indiranagar", "HSR Layout", "Whitefield", "Jayanagar", 
        "JP Nagar", "Electronic City", "BTM Layout", "Marathahalli", "Bellandur", 
        "Malleshwaram", "Hebbal", "Banashankari", "Rajajinagar", "Yelahanka"
    ],
    "bengaluru": [
        "Koramangala", "Indiranagar", "HSR Layout", "Whitefield", "Jayanagar", 
        "JP Nagar", "Electronic City", "BTM Layout", "Marathahalli", "Bellandur", 
        "Malleshwaram", "Hebbal", "Banashankari", "Rajajinagar", "Yelahanka"
    ],
    "hyderabad": [
        "Banjara Hills", "Jubilee Hills", "Hitec City", "Gachibowli", "Madhapur", 
        "Kondapur", "Kukatpally", "Secunderabad", "Begumpet", "Ameerpet", 
        "Dilsukhnagar", "Uppal"
    ],
    "mumbai": [
        "Andheri", "Bandra", "Juhu", "Borivali", "Goregaon", "Malad", 
        "Powai", "Dadar", "Thane", "Navi Mumbai", "Colaba", "Worli"
    ],
    "delhi": [
        "Connaught Place", "South Extension", "Hauz Khas", "Saket", "Dwarka", 
        "Rohini", "Karol Bagh", "Lajpat Nagar", "Janakpuri", "Noida", "Gurgaon"
    ],
    "kochi": [
        "Ernakulam", "Kakkanad", "Edappally", "Fort Kochi", "Aluva", 
        "Tripunithura", "Palarivattom"
    ],
}

def parse_query_location(query: str) -> Tuple[str, str]:
    """
    Extract the business keyword and district/location from a search query.
    Examples:
      'gyms in karur' -> ('gyms', 'karur')
      'dentists in coimbatore' -> ('dentists', 'coimbatore')
      'hotels near madurai' -> ('hotels', 'madurai')
      'salem schools' -> ('schools', 'salem')
    """
    q = query.strip()
    # Check for 'in', 'at', 'near', 'around'
    pattern = r'^(.*?)\s+(?:in|at|near|around)\s+(.*)$'
    match = re.match(pattern, q, re.IGNORECASE)
    if match:
        biz_type = match.group(1).strip()
        location = match.group(2).strip().lower()
        return biz_type, location

    # Check for trailing district name if known
    tokens = q.lower().split()
    for dist in DISTRICT_SUB_AREAS.keys():
        if dist in tokens:
            biz_tokens = [t for t in tokens if t != dist]
            return " ".join(biz_tokens), dist

    # Fallback: whole query as keyword, empty location
    return q, ""

def expand_district_query(query: str, max_sub_areas: int = 15) -> List[str]:
    """
    Expand a query like 'gyms in karur' into a list of sub-area queries.
    Returns:
      [
        'gyms in Karur Central',
        'gyms in Thanthonimalai, Karur',
        'gyms in Gandhigramam, Karur',
        ...
      ]
    """
    biz_type, location = parse_query_location(query)
    if not location:
        # Cannot isolate location, return original query
        return [query]

    # Clean location name
    clean_loc = re.sub(r'[^a-zA-Z\s]', '', location).strip().lower()

    # 1. Exact or partial match in our comprehensive directory
    matched_areas = None
    for dist_key, areas in DISTRICT_SUB_AREAS.items():
        if dist_key == clean_loc or clean_loc in dist_key or dist_key in clean_loc:
            matched_areas = areas
            break

    if matched_areas:
        expanded = []
        for area in matched_areas[:max_sub_areas]:
            if clean_loc in area.lower():
                expanded.append(f"{biz_type} in {area}")
            else:
                expanded.append(f"{biz_type} in {area}, {location.title()}")
        return expanded

    # 2. Smart Directional & Key Hub Expansion for unlisted districts/towns
    loc_title = location.title()
    generic_zones = [
        f"{biz_type} in {loc_title} Central",
        f"{biz_type} in North {loc_title}",
        f"{biz_type} in South {loc_title}",
        f"{biz_type} in East {loc_title}",
        f"{biz_type} in West {loc_title}",
        f"{biz_type} in {loc_title} Main Road",
        f"{biz_type} in {loc_title} Bus Stand",
        f"{biz_type} in {loc_title} Bypass",
        f"{biz_type} in {loc_title} Bazaar",
        f"{biz_type} in {loc_title} Market",
    ]
    return generic_zones[:max_sub_areas]
